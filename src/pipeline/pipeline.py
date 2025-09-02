"""
Activity pipeline:

1. Load data
2. Pull files from S3 (cpu.py)
3. Do GPU inference (gpu.py)
4. Perform alignment and error analysis
5. Update activity fields
"""

import polars as pl
import numpy as np
from loguru import logger
import asyncio
import warnings
from typing import Any
from dataclasses import dataclass
from enum import StrEnum

from src.pipeline.query import (
    load_activity_data,
    load_story_phrase_data,
    merge_activities_with_phrases,
)
warnings.filterwarnings("ignore", category=UserWarning, module=r"torchaudio(\..*)?")
from utils.config import PipelineConfig
from src.pipeline.audio_prep import cpu_download_worker, ActivityOutput, PhraseInput
from src.pipeline.audio_prep.engine import AudioPreparationEngine
from src.pipeline.inference import (
    perform_single_audio_inference,
    GPUInferenceResult,
    PhoneticTranscript,
)
from src.pipeline.inference.engine import preload_inference_engine_async
from my_asr_aligner import word_level_alignment
from infra.appsync_utils import set_activity_fields
from infra.s3_client import preload_s3_client_async, HighPerformanceS3Config


class ActivityFields(StrEnum):
    ACTIVITY_ID = "activityId"
    RECORDS = "records"


def convert_grouped_data_to_phrases(grouped_data: Any) -> list[dict[str, Any]]:
    """
    Convert grouped DataFrame data to list of phrase dictionaries.

    Args:
        grouped_data: The grouped data from polars DataFrame (can be struct or dict)

    Returns:
        List of phrase dictionaries suitable for ActivityInput
    """
    if hasattr(grouped_data, "to_dicts"):
        # If it's a struct, convert to dict
        data_dict = grouped_data.to_dicts()
    else:
        # If it's already a dict (from iter_rows), use it directly
        data_dict = grouped_data

    # Convert from dict of lists to list of dicts
    if isinstance(data_dict, dict):
        # Skip the activityId key
        phrase_keys = [k for k in data_dict.keys() if k != "activityId"]

        if not phrase_keys:
            return []

        # Get the length of the lists (should be the same for all keys)
        list_length = len(data_dict[phrase_keys[0]])

        # Create list of phrase dictionaries
        phrases = []
        for i in range(list_length):
            phrase = {}
            for key in phrase_keys:
                values = data_dict[key]
                phrase[key] = values[i] if i < len(values) else None
            phrases.append(phrase)

        return phrases

    return data_dict if isinstance(data_dict, list) else [data_dict]


@dataclass
class PreparedData:
    """
    Represents the prepared data, including the activity dataframe and the merged phrase dataframe.

    Attributes:
        activity_df: DataFrame containing activity data
        phrase_df: DataFrame containing phrase data
    """

    activity_df: pl.DataFrame
    phrase_df: pl.DataFrame


async def load_and_prepare_data(*, config: PipelineConfig) -> PreparedData:
    """
    Load activity and story phrase data and merge them.

    Args:
        config: Pipeline configuration parameters

    Returns:
        Tuple containing activity dataframe and merged phrase dataframe
    """
    # Kick off async preload of inference engine while we load data
    preload_task = asyncio.create_task(
        preload_inference_engine_async(w2v_config=config.w2v2, warmup=True)
    )
    s3_preload_task = asyncio.create_task(
        preload_s3_client_async(
            config=HighPerformanceS3Config(
                aws_profile=config.aws.aws_profile, aws_region=config.aws.aws_region
            ),
            num_clients=2,
        )
    )

    activity_df: pl.DataFrame = await load_activity_data(config=config)
    story_phrase_df: pl.DataFrame = load_story_phrase_data(config=config)
    phrase_df: pl.DataFrame = await merge_activities_with_phrases(
        activity_df=activity_df, story_phrase_df=story_phrase_df
    )

    logger.info(
        f"{phrase_df.height} phrases from {activity_df.height} activities to process"
    )

    try:
        await asyncio.gather(preload_task, s3_preload_task)
    except Exception as e:
        logger.warning(f"Preload failed (continuing): {e}")

    return PreparedData(activity_df=activity_df, phrase_df=phrase_df)


def group_activities_by_id(*, phrase_df: pl.DataFrame) -> dict[str, list[Any]]:
    """
    Group activities by their IDs and convert to PhraseInput objects.

    Args:
        phrase_df: DataFrame containing phrase data

    Returns:
        Dictionary mapping activity IDs to their PhraseInput records
    """
    logger.info(f"Grouping activities - DataFrame columns: {phrase_df.columns}")
    all_phrases: list[dict[str, Any]] = phrase_df.to_dicts()

    activity_groups: dict[str, list[PhraseInput]] = {}

    for phrase in all_phrases:
        activity_id: str = phrase[ActivityFields.ACTIVITY_ID]
        if activity_id not in activity_groups:
            activity_groups[activity_id] = []

        phrase_obj = PhraseInput.from_dict(phrase)
        activity_groups[activity_id].append(phrase_obj)

    result: dict[str, list[Any]] = {
        ActivityFields.ACTIVITY_ID: list(activity_groups.keys()),
        ActivityFields.RECORDS: list(activity_groups.values()),
    }

    return result


@dataclass
class ProcessedActivity:
    """
    Represents the processed activity, including the activity outputs and the phonetic transcript.

    Attributes:
        activity_outputs: Output from CPU processing
        phonetic_transcript: Phonetic transcript from GPU inference
    """

    activity_outputs: ActivityOutput
    phonetic_transcript: PhoneticTranscript


async def process_activity(
    *, activity_id: str, phrases_input: list[PhraseInput], config: PipelineConfig
) -> ProcessedActivity:
    """
    Process a single activity through CPU and GPU stages.

    Args:
        activity_id: ID of the activity to process
        phrases_input: Input phrases for the activity
        config: Pipeline configuration parameters

    Returns:
        Tuple containing activity output and inference result
    """
    phrase_inputs: list[PhraseInput] = phrases_input
    activity_outputs: ActivityOutput = await cpu_download_worker(
        phrases_input=phrase_inputs, activity_id=activity_id, config=config
    )

    audio_arrays = [
        phrase.speech
        for phrase in activity_outputs.phrases
        if phrase.speech is not None and len(phrase.speech) > 0
    ]
    if audio_arrays:
        concatenated_audio = np.concatenate(audio_arrays, axis=0)
    else:
        raise ValueError("No audio arrays found")

    inference_output: GPUInferenceResult = perform_single_audio_inference(
        audio_array=concatenated_audio, w2v_config=config.w2v2
    )

    return ProcessedActivity(
        activity_outputs=activity_outputs,
        phonetic_transcript=inference_output.phonetic_transcript,
    )


def perform_alignment(
    *, activity_outputs: ActivityOutput, phonetic_transcript: PhoneticTranscript
) -> Any:
    """
    Perform alignment and error analysis.

    Args:
        activity_outputs: Output from CPU processing
        phonetic_transcript: Phonetic transcript from GPU inference

    Returns:
        Dictionary containing alignment errors nested by phrase
    """
    # Process alignment for each phrase individually, maintaining nested structure
    phrase_errors_nested = []
    
    for phrase in activity_outputs.phrases:
        expected_text: list[str] = phrase.expected_text if isinstance(phrase.expected_text, list) else [phrase.expected_text]
        reference_phonemes: list[str] = phrase.reference_phonemes if isinstance(phrase.reference_phonemes, list) else [phrase.reference_phonemes]
        
        try:
            _, phrase_errors, _ = word_level_alignment(
                expected_items=expected_text,
                ref_phons=reference_phonemes,
                hyp_phons=phonetic_transcript.elements,
                confidences=phonetic_transcript.confidences
            )
            phrase_errors_nested.append(phrase_errors)
        except Exception as e:
            logger.error(f"Alignment failed for phrase {phrase}: {e}")
            # Add errors for all expected items in this phrase
            phrase_errors_nested.append([True] * len(expected_text))
    
    # Flatten for summary statistics but keep nested structure for main output
    flattened_errors = [error for phrase_errors in phrase_errors_nested for error in phrase_errors]

    logger.info(f"Alignment errors by phrase: {phrase_errors_nested}")
    
    # Convert alignment errors to proper field values format
    error_count = sum(flattened_errors) if flattened_errors else 0
    total_count = len(flattened_errors) if flattened_errors else 0
    accuracy = (total_count - error_count) / total_count if total_count > 0 else 0.0
    
    field_values = {
        "alignment_errors": phrase_errors_nested,
        "error_count": error_count,
        "total_alignments": total_count,
        "alignment_accuracy": accuracy
    }
    
    return field_values


async def process_single_activity(
    *, activity_id: str, phrases_input: list[PhraseInput], config: PipelineConfig
) -> dict[str, Any]:
    """
    Process a single activity through CPU and GPU stages.
    """
    processed_activity: ProcessedActivity = await process_activity(
        activity_id=activity_id,
        phrases_input=phrases_input,
        config=config,
    )

    errors: Any = perform_alignment(
        activity_outputs=processed_activity.activity_outputs,
        phonetic_transcript=processed_activity.phonetic_transcript,
    )

    activity_response: dict[str, Any] = set_activity_fields(
        activity_id=activity_id, field_values=errors, config=config.aws
    )

    return activity_response


async def run_activity_pipeline(*, config: PipelineConfig) -> list[dict[str, Any]]:
    """
    Run the streamlined activity pipeline.

    Args:
        config: Pipeline configuration parameters

    Returns:
        bool: True if pipeline executed successfully
    """
    prepared_data: PreparedData = await load_and_prepare_data(config=config)

    activity_groups = group_activities_by_id(
        phrase_df=prepared_data.phrase_df
    )
    activity_ids = activity_groups.get(ActivityFields.ACTIVITY_ID, [])

    if not activity_ids:
        logger.warning("No activity IDs found to process")
        return []

    activity_responses: list[dict[str, Any]] = []
    records_list = activity_groups.get(
        ActivityFields.RECORDS, []
    )

    logger.debug(f"Processing {len(activity_ids)} activities")
    logger.debug(f"Records list length: {len(records_list)}")
    for idx, activity_id in enumerate(activity_ids):
        if idx < len(records_list):
            activity_response: dict[str, Any] = await process_single_activity(
                activity_id=activity_id,
                phrases_input=records_list[idx],
                config=config,
            )
            activity_responses.append(activity_response)
        else:
            logger.warning(f"No records found for activity {activity_id}")
    # Best-effort close of S3 sessions used by the audio prep engine
    try:
        # cpu_download_worker constructs and closes its own S3 clients internally in many cases,
        # but if an engine is used externally, expose a close hook.
        engine = AudioPreparationEngine(config=config)
        await engine.close()
    except Exception as e:
        logger.warning(f"S3 cleanup skipped: {e}")

    return activity_responses
