"""
Activity pipeline:

1. Load data
2. Pull files from S3 (cpu.py)
3. Do GPU inference (gpu.py)
4. Perform alignment and error analysis
5. Update activity fields
"""

import polars as pl
from loguru import logger
from typing import Any
from dataclasses import dataclass
from enum import StrEnum

from src.pipeline.query import (
    load_activity_data,
    load_story_phrase_data,
    merge_activities_with_phrases,
)
from utils.config import PipelineConfig
from src.pipeline.audio_prep import cpu_download_worker, ActivityOutput
from src.pipeline.inference import (
    perform_single_audio_inference,
    GPUInferenceResult,
    PhoneticTranscript,
)
from my_asr_aligner import word_level_alignment
from infra.appsync_utils import set_activity_fields


class ActivityFields(StrEnum):
    ACTIVITY_ID = "activityId"
    RECORDS = "records"


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
    activity_df: pl.DataFrame = await load_activity_data(config=config)
    story_phrase_df: pl.DataFrame = load_story_phrase_data(config=config)
    phrase_df: pl.DataFrame = await merge_activities_with_phrases(
        activity_df=activity_df, story_phrase_df=story_phrase_df
    )

    logger.info(
        f"{phrase_df.height} phrases from {activity_df.height} activities to process"
    )

    return PreparedData(activity_df=activity_df, phrase_df=phrase_df)


def group_activities_by_id(*, phrase_df: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """
    Group activities by their IDs.

    Args:
        phrase_df: DataFrame containing phrase data

    Returns:
        Dictionary mapping activity IDs to their records
    """
    return (
        phrase_df.group_by(ActivityFields.ACTIVITY_ID)
        .agg(pl.all().alias(ActivityFields.RECORDS))
        .select(
            ActivityFields.ACTIVITY_ID,
            pl.col(ActivityFields.RECORDS).map_elements(lambda x: x.to_dicts()),
        )
        .to_dict(as_series=False)
    )


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


def process_activity(
    *, activity_id: str, phrases_input: list[dict[str, Any]], config: PipelineConfig
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
    activity_outputs: ActivityOutput = cpu_download_worker(
        phrases_input=phrases_input, activity_id=activity_id, config=config
    )

    inference_output: GPUInferenceResult = perform_single_audio_inference(
        audio_array=activity_outputs.phrases, config=config
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
        Dictionary containing alignment errors
    """
    expected_text: list[str] = [
        phrase.expected_text for phrase in activity_outputs.phrases
    ]
    reference_phonemes: list[str] = [
        phrase.reference_phonemes for phrase in activity_outputs.phrases
    ]
    transcript_phonemes: list[str] = phonetic_transcript.elements
    transcript_confidences: list[float] = phonetic_transcript.confidences

    _, errors, _ = word_level_alignment(
        expected_text, reference_phonemes, transcript_phonemes, transcript_confidences
    )

    logger.info(f"Alignment errors: {errors}")
    return errors


def process_single_activity(
    *, activity_id: str, phrases_input: list[dict[str, Any]], config: PipelineConfig
) -> dict[str, Any]:
    """
    Process a single activity through CPU and GPU stages.
    """
    processed_activity: ProcessedActivity = process_activity(
        activity_id=activity_id,
        phrases_input=phrases_input,
        config=config,
    )

    errors: Any = perform_alignment(
        activity_outputs=processed_activity.activity_outputs,
        phonetic_transcript=processed_activity.phonetic_transcript,
    )

    activity_response: dict[str, Any] = set_activity_fields(
        activity_id=activity_id, field_values=errors, config=config
    )

    return activity_response


async def run_activity_pipeline(config: PipelineConfig) -> list[dict[str, Any]]:
    """
    Run the streamlined activity pipeline.

    Args:
        config: Pipeline configuration parameters

    Returns:
        bool: True if pipeline executed successfully
    """
    prepared_data: PreparedData = await load_and_prepare_data(config=config)

    activity_groups: dict[str, dict[str, Any]] = group_activities_by_id(
        phrase_df=prepared_data.phrase_df
    )
    activity_ids: list[str] = activity_groups.get(ActivityFields.ACTIVITY_ID, [])

    if not activity_ids:
        logger.warning("No activity IDs found to process")
        return []

    activity_responses: list[dict[str, Any]] = []
    for activity_id in activity_ids:
        activity_response: dict[str, Any] = process_single_activity(
            activity_id=activity_id,
            phrases_input=activity_groups[ActivityFields.RECORDS][activity_id],
            config=config,
        )
        activity_responses.append(activity_response)

    return activity_responses
