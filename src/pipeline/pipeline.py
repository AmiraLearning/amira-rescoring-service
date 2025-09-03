"""
Activity pipeline:

1. Load data
2. Pull files from S3 (cpu.py)
3. Do GPU inference (gpu.py)
4. Perform alignment and error analysis
5. Update activity fields
"""
# TODO too monolithic and mixed with bare dicts

import asyncio
import os
import time
import warnings
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
import polars as pl
from loguru import logger

from infra.s3_client import HighPerformanceS3Config, preload_s3_client_async
from src.pipeline.audio_prep import ActivityOutput, PhraseInput, cpu_download_worker
from src.pipeline.inference import (
    GPUInferenceResult,
    PhoneticTranscript,
    perform_single_audio_inference,
)
from src.pipeline.inference.engine import preload_inference_engine_async
from src.pipeline.query import (
    load_activity_data,
    load_story_phrase_data,
    merge_activities_with_phrases,
)
from utils.config import PipelineConfig
from utils.logging import emit_emf_metric

from .inference.metrics_constants import (
    DIM_ACTIVITY_ID,
    DIM_CORRELATION_ID,
    MET_ACTIVITY_PHRASES,
    MET_ACTIVITY_SUCCESS,
    MET_ACTIVITY_TOTAL_MS,
    MET_ALIGN_ACCURACY,
    MET_ALIGN_ERROR_COUNT,
    MET_ALIGN_TOTAL,
    NS_ACTIVITY,
    NS_ALIGNMENT,
)

warnings.filterwarnings("ignore", category=UserWarning, module=r"torchaudio(\..*)?$")


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
    data_dict = grouped_data.to_dicts() if hasattr(grouped_data, "to_dicts") else grouped_data
    if isinstance(data_dict, dict):
        phrase_keys = [k for k in data_dict.keys() if k != "activityId"]

        if not phrase_keys:
            return []

        list_length = len(data_dict[phrase_keys[0]])

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
        engine: Pre-loaded inference engine (optional)
    """

    activity_df: pl.DataFrame
    phrase_df: pl.DataFrame
    engine: Any = None


async def load_and_prepare_data(
    *, config: PipelineConfig | None, cached_engine: Any = None
) -> PreparedData:
    """
    Load activity and story phrase data and merge them.

    Args:
        config: Pipeline configuration parameters
        cached_engine: Pre-cached engine instance (optional)

    Returns:
        PreparedData containing activity dataframe and merged phrase dataframe

    Raises:
        ValueError: If config is None or invalid
        Exception: If data loading fails
    """
    if config is None:
        raise ValueError("Config parameter is required")

    # Skip engine preloading if we already have a cached engine
    if cached_engine is not None:
        logger.info("Using cached engine, skipping preload")
        s3_preload_task: asyncio.Task[Any] = asyncio.create_task(
            preload_s3_client_async(
                config=HighPerformanceS3Config(
                    aws_profile=config.aws.aws_profile,
                    aws_region=config.aws.aws_region,
                ),
                num_clients=int(os.getenv("S3_WARM_CLIENTS", "2")),
            )
        )

        activity_df: pl.DataFrame = await load_activity_data(config=config)
        story_phrase_df: pl.DataFrame = load_story_phrase_data(config=config)
        phrase_df: pl.DataFrame = await merge_activities_with_phrases(
            activity_df=activity_df, story_phrase_df=story_phrase_df
        )

        logger.info(f"{phrase_df.height} phrases from {activity_df.height} activities to process")

        try:
            await s3_preload_task
        except Exception as e:
            logger.warning(f"S3 preload failed (continuing): {e}")

        return PreparedData(activity_df=activity_df, phrase_df=phrase_df, engine=cached_engine)

    # Original behavior for when no cached engine is provided
    preload_task: asyncio.Task[Any] = asyncio.create_task(
        preload_inference_engine_async(w2v_config=config.w2v2, warmup=True)
    )
    s3_preload_task_fallback: asyncio.Task[Any] = asyncio.create_task(
        preload_s3_client_async(
            config=HighPerformanceS3Config(
                aws_profile=config.aws.aws_profile,
                aws_region=config.aws.aws_region,
            ),
            num_clients=int(os.getenv("S3_WARM_CLIENTS", "2")),
        )
    )

    activity_df_fallback: pl.DataFrame = await load_activity_data(config=config)
    story_phrase_df_fallback: pl.DataFrame = load_story_phrase_data(config=config)
    phrase_df_fallback: pl.DataFrame = await merge_activities_with_phrases(
        activity_df=activity_df_fallback, story_phrase_df=story_phrase_df_fallback
    )

    logger.info(
        f"{phrase_df_fallback.height} phrases from {activity_df_fallback.height} activities to process"
    )

    try:
        engine, _ = await asyncio.gather(preload_task, s3_preload_task_fallback)
    except Exception as e:
        logger.warning(f"Preload failed (continuing): {e}")
        engine = None

    return PreparedData(
        activity_df=activity_df_fallback, phrase_df=phrase_df_fallback, engine=engine
    )


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
    *,
    activity_id: str,
    phrases_input: list[PhraseInput],
    config: PipelineConfig | None,
    engine: Any = None,
) -> ProcessedActivity:
    """
    Process a single activity through CPU and GPU stages.

    Args:
        activity_id: ID of the activity to process
        phrases_input: Input phrases for the activity
        config: Pipeline configuration parameters
        engine: Pre-loaded inference engine (optional)

    Returns:
        ProcessedActivity containing activity output and phonetic transcript

    Raises:
        ValueError: If required parameters are None or invalid
        Exception: If processing fails
    """
    if not activity_id or not activity_id.strip():
        raise ValueError("Activity ID cannot be empty")

    if not phrases_input:
        raise ValueError("Phrases input cannot be empty")

    if config is None:
        raise ValueError("Config parameter is required")
    phrase_inputs: list[PhraseInput] = phrases_input
    activity_outputs: ActivityOutput = await cpu_download_worker(
        phrases_input=phrase_inputs, activity_id=activity_id, config=config
    )

    all_elements = []
    all_confidences = []

    engine_local = engine
    if engine_local is None:
        try:
            if config.w2v2.use_triton:
                from src.pipeline.inference.triton_engine import TritonInferenceEngine

                engine_local = TritonInferenceEngine(w2v_config=config.w2v2)
            else:
                from src.pipeline.inference.engine import Wav2Vec2InferenceEngine

                engine_local = Wav2Vec2InferenceEngine(w2v_config=config.w2v2)
        except Exception as e:
            logger.warning(f"Failed to initialize inference engine on-demand: {e}")
            engine_local = None

    correlation_id = (
        str(config.metadata.correlation_id)
        if hasattr(config, "metadata")
        and hasattr(config.metadata, "correlation_id")
        and config.metadata.correlation_id
        else None
    )

    for phrase in activity_outputs.phrases:
        phrase_start_time: float = time.time()
        if phrase.speech is not None and len(phrase.speech) > 0:
            logger.debug(
                f"Phrase {phrase.phraseIndex}: audio length {len(phrase.speech)}, min/max {np.min(phrase.speech):.3f}/{np.max(phrase.speech):.3f}"
            )
            inference_output: GPUInferenceResult = perform_single_audio_inference(
                audio_array=phrase.speech,
                w2v_config=config.w2v2,
                inference_id=f"{activity_id}_phrase_{phrase.phraseIndex}",
                engine=engine_local,
                use_cache=True,  # Enable caching for Lambda optimization
                correlation_id=correlation_id,
            )
            if correlation_id is not None:
                inference_output.inference_id = correlation_id

            if inference_output.success and inference_output.phonetic_transcript:
                logger.debug(
                    f"Phrase {phrase.phraseIndex}: transcribed to {inference_output.phonetic_transcript.elements}"
                )
                all_elements.extend(inference_output.phonetic_transcript.elements)
                all_confidences.extend(inference_output.phonetic_transcript.confidences)
                try:
                    emit_emf_metric(
                        namespace="Amira/Phrase",
                        metrics={
                            "PhraseProcessingMs": (time.time() - phrase_start_time) * 1000.0,
                            "PhraseSuccess": 1.0,
                        },
                        dimensions={
                            "ActivityId": str(activity_id),
                            "PhraseIndex": str(phrase.phraseIndex),
                        },
                    )
                except Exception:
                    pass
            else:
                logger.warning(f"Phrase {phrase.phraseIndex}: inference failed")
                try:
                    emit_emf_metric(
                        namespace="Amira/Phrase",
                        metrics={
                            "PhraseProcessingMs": (time.time() - phrase_start_time) * 1000.0,
                            "PhraseSuccess": 0.0,
                        },
                        dimensions={
                            "ActivityId": str(activity_id),
                            "PhraseIndex": str(phrase.phraseIndex),
                        },
                    )
                except Exception:
                    pass

    from src.pipeline.inference.models import PhoneticTranscript

    combined_transcript = PhoneticTranscript(elements=all_elements, confidences=all_confidences)

    return ProcessedActivity(
        activity_outputs=activity_outputs,
        phonetic_transcript=combined_transcript,
    )


def perform_alignment(
    *,
    activity_outputs: ActivityOutput | None,
    phonetic_transcript: PhoneticTranscript | None,
    enable_confidence_weighting: bool = False,
) -> Any:
    """
    Perform alignment and error analysis.

    Args:
        activity_outputs: Output from CPU processing
        phonetic_transcript: Phonetic transcript from GPU inference

    Returns:
        Dictionary containing alignment errors nested by phrase

    Raises:
        ValueError: If required parameters are None or invalid
        Exception: If alignment processing fails
    """
    if activity_outputs is None:
        raise ValueError("Activity outputs parameter is required")

    if phonetic_transcript is None:
        raise ValueError("Phonetic transcript parameter is required")

    if not activity_outputs.phrases:
        logger.warning("No phrases found in activity outputs")
        return {
            "alignment_errors": [],
            "error_count": 0,
            "total_alignments": 0,
            "alignment_accuracy": 0.0,
        }
    all_expected_text = []
    all_reference_phonemes = []

    for phrase in activity_outputs.phrases:
        expected_text: list[str] = (
            phrase.expected_text
            if isinstance(phrase.expected_text, list)
            else [phrase.expected_text]
        )
        reference_phonemes: list[str] = (
            phrase.reference_phonemes
            if isinstance(phrase.reference_phonemes, list)
            else [phrase.reference_phonemes]
        )

        all_expected_text.extend(expected_text)
        all_reference_phonemes.extend(reference_phonemes)

    logger.info(f"Expected items (first 10): {all_expected_text[:10]}")
    logger.info(f"Reference phonemes (first 10): {all_reference_phonemes[:10]}")
    logger.info(f"Hypothesis phonemes (first 10): {phonetic_transcript.elements[:10]}")
    logger.info(
        f"Total expected: {len(all_expected_text)}, ref: {len(all_reference_phonemes)}, hyp: {len(phonetic_transcript.elements)}"
    )

    try:
        from my_asr_aligner import word_level_alignment  # type: ignore

        _, flat_errors, _ = word_level_alignment(
            expected_items=all_expected_text,
            ref_phons=all_reference_phonemes,
            hyp_phons=phonetic_transcript.elements,
            confidences=phonetic_transcript.confidences,
            enable_confidence_weighting=enable_confidence_weighting,
        )

        phrase_errors_nested: list[list[bool]] = []
        error_idx: int = 0
        for phrase in activity_outputs.phrases:
            phrase_expected_text: list[str] = (
                phrase.expected_text
                if isinstance(phrase.expected_text, list)
                else [phrase.expected_text]
            )
            phrase_len: int = len(phrase_expected_text)
            phrase_errors: list[bool] = flat_errors[error_idx : error_idx + phrase_len]
            phrase_errors_nested.append(phrase_errors)
            error_idx += phrase_len

    except Exception as e:
        logger.error(f"Alignment failed for activity: {e}")
        try:
            emit_emf_metric(
                namespace="Amira/Activity",
                metrics={"AlignFailure": 1.0},
                dimensions={},
            )
        except Exception:
            pass
        phrase_errors_nested = [
            [True]
            * len(
                phrase.expected_text
                if isinstance(phrase.expected_text, list)
                else [phrase.expected_text]
            )
            for phrase in activity_outputs.phrases
        ]

    flattened_errors: list[bool] = [
        error for phrase_errors in phrase_errors_nested for error in phrase_errors
    ]

    logger.info(f"Alignment errors by phrase: {phrase_errors_nested}")

    error_count: int = sum(flattened_errors) if flattened_errors else 0
    total_count: int = len(flattened_errors) if flattened_errors else 0
    accuracy: float = (total_count - error_count) / total_count if total_count > 0 else 0.0

    field_values: dict[str, Any] = {
        "alignment_errors": phrase_errors_nested,
        "error_count": error_count,
        "total_alignments": total_count,
        "alignment_accuracy": accuracy,
    }

    try:
        emit_emf_metric(
            namespace=NS_ALIGNMENT,
            metrics={
                MET_ALIGN_ERROR_COUNT: float(error_count),
                MET_ALIGN_TOTAL: float(total_count),
                MET_ALIGN_ACCURACY: float(accuracy),
            },
            dimensions={},
        )
    except Exception:
        pass

    return field_values


async def process_single_activity(
    *,
    activity_id: str,
    phrases_input: list[PhraseInput],
    config: PipelineConfig,
    engine: Any = None,
) -> dict[str, Any]:
    """
    Process a single activity through CPU and GPU stages.
    """
    activity_start: float = time.time()
    try:
        processed_activity: ProcessedActivity = await process_activity(
            activity_id=activity_id,
            phrases_input=phrases_input,
            config=config,
            engine=engine,
        )
    except Exception:
        try:
            correlation_id = (
                str(config.metadata.correlation_id)
                if hasattr(config, "metadata")
                and hasattr(config.metadata, "correlation_id")
                and config.metadata.correlation_id
                else None
            )
            emit_emf_metric(
                namespace=NS_ACTIVITY,
                metrics={
                    MET_ACTIVITY_SUCCESS: 0.0,
                },
                dimensions={
                    DIM_ACTIVITY_ID: str(activity_id),
                    **({DIM_CORRELATION_ID: correlation_id} if correlation_id is not None else {}),
                },
            )
        except Exception:
            pass
        raise

    errors: Any = perform_alignment(
        activity_outputs=processed_activity.activity_outputs,
        phonetic_transcript=processed_activity.phonetic_transcript,
        enable_confidence_weighting=getattr(config, "enable_confidence_weighting", False),
    )

    total_ms: float = (time.time() - activity_start) * 1_000.0
    try:
        correlation_id = (
            str(config.metadata.correlation_id)
            if hasattr(config, "metadata")
            and hasattr(config.metadata, "correlation_id")
            and config.metadata.correlation_id
            else None
        )
        phrase_count: int = len(processed_activity.activity_outputs.phrases)
        emit_emf_metric(
            namespace=NS_ACTIVITY,
            metrics={
                MET_ACTIVITY_TOTAL_MS: total_ms,
                MET_ACTIVITY_PHRASES: float(phrase_count),
                MET_ACTIVITY_SUCCESS: 1.0,
            },
            dimensions={
                DIM_ACTIVITY_ID: str(activity_id),
                **({DIM_CORRELATION_ID: correlation_id} if correlation_id is not None else {}),
            },
        )
    except Exception:
        pass

    activity_response: dict[str, Any] = {}
    try:
        from infra.appsync_utils import ActivityFieldValues, set_activity_fields

        fields: ActivityFieldValues = ActivityFieldValues(
            alignment_errors=errors.get("alignment_errors"),
            error_count=errors.get("error_count"),
            total_alignments=errors.get("total_alignments"),
            alignment_accuracy=errors.get("alignment_accuracy"),
        )
        activity_response = set_activity_fields(
            activity_id=activity_id,
            field_values=fields.model_dump(),
            config=config.aws,
            correlation_id=correlation_id,
        )
        logger.info(f"AppSync fields updated successfully for activity {activity_id}")
    except Exception as e:
        logger.info(f"Skipping AppSync upload - using local response due to: {e}")
        logger.info(f"Would set fields: {errors}")
        activity_response = {"data": {"updateActivity": {"activityId": activity_id}}}

    return activity_response


async def run_activity_pipeline(
    *, config: PipelineConfig, cached_engine: Any = None
) -> list[dict[str, Any]]:
    """
    Run the streamlined activity pipeline.

    Args:
        config: Pipeline configuration parameters
        cached_engine: Pre-cached engine instance (optional)

    Returns:
        bool: True if pipeline executed successfully
    """
    prepared_data: PreparedData = await load_and_prepare_data(
        config=config, cached_engine=cached_engine
    )
    engine = prepared_data.engine

    activity_groups = group_activities_by_id(phrase_df=prepared_data.phrase_df)
    activity_ids = activity_groups.get(ActivityFields.ACTIVITY_ID, [])

    if not activity_ids:
        logger.warning("No activity IDs found to process")
        return []

    activity_responses: list[dict[str, Any]] = []
    records_list = activity_groups.get(ActivityFields.RECORDS, [])

    logger.debug(f"Processing {len(activity_ids)} activities")
    logger.debug(f"Records list length: {len(records_list)}")

    max_concurrency_env = os.getenv("PIPELINE_MAX_CONCURRENCY", "4")
    try:
        max_concurrency: int = max(1, int(max_concurrency_env))
    except Exception:
        max_concurrency = 4

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_one(idx: int, activity_id: str) -> dict[str, Any]:
        async with semaphore:
            if idx < len(records_list):
                return await process_single_activity(
                    activity_id=activity_id,
                    phrases_input=records_list[idx],
                    config=config,
                    engine=engine,
                )
            logger.warning(f"No records found for activity {activity_id}")
            return {}

    tasks = [_run_one(idx, aid) for idx, aid in enumerate(activity_ids)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict) and r:
            activity_responses.append(r)
    try:
        from infra.s3_client import close_global_s3_clients_async

        await close_global_s3_clients_async()
    except Exception as e:
        logger.warning(f"S3 cleanup skipped: {e}")

    return activity_responses
