"""
Core processing logic for the ORF rescoring pipeline.

This module contains the core processing functions for handling individual activities
in a Lambda-based serverless environment. Each function processes one activity
independently without batch operations.
"""

import logging
from typing import TYPE_CHECKING, Any, Final

import amira_pyutils.services.s3 as s3_utils

from orf_rescoring_pipeline.alignment.audio_processing import (
    load_activity_audio_data_from_s3,
)
from orf_rescoring_pipeline.alignment.phrase_alignment import process_activity_timing
from orf_rescoring_pipeline.alignment.phrase_manifest import (
    PhraseBuilder,
    PhraseManifest,
)
from orf_rescoring_pipeline.alignment.word_alignment import (
    get_word_level_transcript_alignment,
)
from orf_rescoring_pipeline.models import Activity
from orf_rescoring_pipeline.rules.flagging import FlaggingAnalyzer, FlaggingExecutor
from orf_rescoring_pipeline.utils.debug import ActivityDebugger
from orf_rescoring_pipeline.utils.file_operations import trim_predictions
from utils.standardized_metrics import emit_standardized_metric

if TYPE_CHECKING:
    from amira_pyutils.general.environment import Environment
    from amira_pyutils.services.appsync import AppSync

    from orf_rescoring_pipeline.utils.transcription import (
        DeepgramASRClient,
        KaldiASRClient,
        W2VASRClient,
    )

logger = logging.getLogger(__name__)

REQUIRED_FEATURE_FIELDS: Final[list[str]] = [
    "model",
    "phraseIndex",
    "Kaldi_match",
    "W2V_match",
    "we_dist",
    "correct_confidences",
]

APPSYNC_UPDATE_FIELDS: Final[dict[str, str]] = {
    "status": "rescored",
    "displayStatus": "rescored",
}


def process_single_activity(
    activity: Activity,
    appsync: "AppSync",
    env: "Environment",
    deepgram: "DeepgramASRClient",
    kaldi: "KaldiASRClient",
    w2v: "W2VASRClient",
    debug: bool = False,
    save_files: bool = False,
    model_features_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """
    Process a single activity through the complete rescoring pipeline.

    Args:
        activity: Activity object to process
        appsync: AppSync client for GraphQL operations
        env: Environment configuration
        deepgram: Deepgram ASR client
        kaldi: Kaldi ASR client
        w2v: W2V ASR client
        debug: If True, creates detailed debug output file for this activity
        save_files: If True, saves intermediate files to disk
        model_features_cache: Pre-loaded model features to avoid per-activity queries

    Returns:
        For debug mode: (success, activity_id, accuracy_stats_tuple)
        For normal mode: (success, activity_id)
    """
    try:
        import time

        t0 = time.time()
        _load_model_features_into_activity(
            activity=activity,
            appsync=appsync,
            model_features_cache=model_features_cache,
        )
        t1 = time.time()

        _execute_audio_transcription_phase(
            activity=activity,
            env=env,
            deepgram=deepgram,
            save_files=save_files,
        )
        t2 = time.time()

        _execute_word_alignment_phase(activity=activity)
        t3 = time.time()

        result = _execute_flagging_rescoring_phase(
            activity=activity,
            appsync=appsync,
            kaldi=kaldi,
            w2v=w2v,
            debug=debug,
        )
        t4 = time.time()

        try:
            phrase_count = sum(len(p.phrases) for p in activity.pages) if activity.pages else 0
            emit_standardized_metric(
                namespace="Amira/ORFRescore",
                metrics={
                    "ModelFeaturesLoadMs": (t1 - t0) * 1000.0,
                    "AudioTranscriptionMs": (t2 - t1) * 1000.0,
                    "WordAlignmentMs": (t3 - t2) * 1000.0,
                    "RescoringMs": (t4 - t3) * 1000.0,
                    "ActivitySuccess": 1.0,
                    "PhraseCount": float(phrase_count),
                },
                activity_id=str(activity.activity_id),
                additional_dimensions={"Pipeline": "ORFRescore"},
            )
        except Exception:
            pass

        return result

    except Exception as e:
        logger.error(f"Error processing activity {activity.activity_id}: {e}")
        try:
            emit_standardized_metric(
                namespace="Amira/ORFRescore",
                metrics={"ActivitySuccess": 0.0},
                activity_id=str(activity.activity_id),
                additional_dimensions={"Pipeline": "ORFRescore"},
            )
        except Exception:
            pass
        return _create_error_response(activity_id=activity.activity_id, debug=debug)


def _load_model_features_into_activity(
    *,
    activity: Activity,
    appsync: "AppSync",
    model_features_cache: dict[str, list[dict[str, Any]]] | None,
) -> None:
    """Load model features from cache or fetch from AppSync."""
    if model_features_cache and activity.activity_id in model_features_cache:
        model_features = model_features_cache[activity.activity_id]
    else:
        model_features = _fetch_model_features_from_appsync(
            activity_id=activity.activity_id, appsync=appsync
        )

    activity.model_features_from_appsync_res(appsync_model_res=model_features)


def _fetch_model_features_from_appsync(
    *, activity_id: str, appsync: "AppSync"
) -> list[dict[str, Any]]:
    """Fetch model features from AppSync for the given activity."""
    features_query = appsync.get_custom_activity_features(feature_list=REQUIRED_FEATURE_FIELDS)
    from typing import cast

    return cast(list[dict[str, Any]], features_query(activity_id=activity_id))


def _execute_audio_transcription_phase(
    *,
    activity: Activity,
    env: "Environment",
    deepgram: "DeepgramASRClient",
    save_files: bool,
) -> None:
    """Execute Phase 1: Audio loading and transcription processing."""
    load_activity_audio_data_from_s3(activity=activity, audio_bucket=env.audio_bucket)

    phrase_manifest = _create_phrase_manifest()
    pages = phrase_manifest.generate(bucket=env.audio_bucket, activity_id=activity.activity_id)

    import asyncio

    try:
        loop = asyncio.get_event_loop()
        activity.transcript_json = loop.run_until_complete(
            deepgram.transcribe_async(activity, save_transcript=save_files)
        )
    except Exception:
        # Fall back to sync transcription
        activity.transcript_json = deepgram.transcribe(activity, save_transcript=save_files)

    activity.load_page_data()
    process_activity_timing(activity=activity, manifest_pages=pages, save_files=save_files)


def _create_phrase_manifest() -> PhraseManifest:
    """Create a phrase manifest with S3 client."""
    builder = PhraseBuilder(s3_client=s3_utils.get_client())
    return PhraseManifest(builder=builder)


def _execute_word_alignment_phase(*, activity: Activity) -> None:
    """Execute Phase 2: Word-level alignment with Deepgram."""
    features = activity.preferred_model_features

    for page in activity.pages:
        for phrase in page.aligned_phrases or []:
            absolute_phrase_idx = page.phrase_indices[phrase["phrase_idx"]]

            _validate_feature_availability(
                activity=activity, absolute_phrase_idx=absolute_phrase_idx
            )

            deepgram_matches = get_word_level_transcript_alignment(
                story_phrase=page.phrases[phrase["phrase_idx"]],
                transcript_text=phrase["parsed"],
            )

            _update_feature_with_deepgram_matches(
                activity=activity,
                features=features,
                absolute_phrase_idx=absolute_phrase_idx,
                deepgram_matches=deepgram_matches,
            )


def _validate_feature_availability(*, activity: Activity, absolute_phrase_idx: int) -> None:
    """Validate that feature data is available for the given phrase index."""
    feature_indices = [feature.phrase_index for feature in activity.preferred_model_features]

    if absolute_phrase_idx not in feature_indices:
        if absolute_phrase_idx > activity.max_feature_index:
            logger.warning(
                f"Phrase {absolute_phrase_idx} is after the last feature index "
                f"{activity.max_feature_index} for {activity.activity_id}"
            )
        else:
            error_msg = (
                f"Feature data is missing for {activity.activity_id}, phrase:{absolute_phrase_idx}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)


def _update_feature_with_deepgram_matches(
    *,
    activity: Activity,
    features: list[Any],
    absolute_phrase_idx: int,
    deepgram_matches: list[Any],
) -> None:
    """Update feature with Deepgram matches and validate alignment."""
    features[absolute_phrase_idx].deepgram_match = deepgram_matches

    expected_length = len(activity.model_predictions[absolute_phrase_idx])
    actual_length = len(deepgram_matches)

    if actual_length != expected_length:
        error_msg = (
            f"Deepgram match length {actual_length} does not match "
            f"model predictions length {expected_length} for "
            f"{activity.activity_id}, phrase:{absolute_phrase_idx} "
            f"({features[absolute_phrase_idx].model})"
        )
        raise AssertionError(error_msg)


def _execute_flagging_rescoring_phase(
    *,
    activity: Activity,
    appsync: "AppSync",
    kaldi: "KaldiASRClient",
    w2v: "W2VASRClient",
    debug: bool,
) -> Any:
    """Execute Phase 3: Flagging and rescoring logic.

    Args:
        activity: Activity object to process
        appsync: AppSync client for GraphQL operations
        kaldi: Kaldi ASR client
        w2v: W2V ASR client
        debug: If True, creates detailed debug output file for this activity

    Returns:
        For debug mode: (success, activity_id, accuracy_stats_tuple)
        For normal mode: (success, activity_id)
    """
    _truncate_errors_to_available_pages(activity=activity)

    debugger = ActivityDebugger(activity=activity) if debug else None
    flagging_executor = FlaggingExecutor(activity=activity, kaldi=kaldi, w2v=w2v, debugger=debugger)

    _process_features_with_flagging(activity=activity, flagging_executor=flagging_executor)

    activity.errors_retouched = trim_predictions(predictions=activity.errors_retouched)

    if not debug:
        _update_activity_in_appsync(activity=activity, appsync=appsync)

    if debug and debugger is not None:
        _save_debug_output(activity=activity, debugger=debugger)
        return _create_debug_response(activity=activity, debugger=debugger)

    return True, activity.activity_id


def _truncate_errors_to_available_pages(*, activity: Activity) -> None:
    """Limit errors array to the pages and phrases that exist in audio segments.

    Args:
        activity: Activity object to process
    """
    max_phrase_idx = max([max(page.phrase_indices) for page in activity.pages])

    if len(activity.errors) > max_phrase_idx + 1:
        original_length = len(activity.errors)
        activity.errors = activity.errors[: max_phrase_idx + 1]
        logger.debug(
            f"Truncated errors for {activity.activity_id} from "
            f"{original_length} to {max_phrase_idx + 1}"
        )


def _process_features_with_flagging(
    *, activity: Activity, flagging_executor: FlaggingExecutor
) -> None:
    """Process each feature with flagging logic.

    Args:
        activity: Activity object to process
        flagging_executor: FlaggingExecutor object to process features
    """
    for feature in activity.preferred_model_features:
        errors = activity.errors[feature.phrase_index]
        errors = activity.pad_to_phrase_length(errors=errors, phrase_index=feature.phrase_index)

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=errors)
        retouched_errors = flagging_executor.execute_flagging_decision(
            feature=feature, decision=decision
        )
        activity.errors_retouched.append(retouched_errors)


def _update_activity_in_appsync(*, activity: Activity, appsync: "AppSync") -> None:
    """Update the activity status in AppSync.

    Args:
        activity: Activity object to process
        appsync: AppSync client for GraphQL operations
    """
    try:
        field_values = {
            "errors": activity.errors_retouched,
            **APPSYNC_UPDATE_FIELDS,
        }

        appsync.set_activity_fields(
            activity_id=activity.activity_id,
            field_values=field_values,
        )
        logger.info(f"Updated activity {activity.activity_id} in AppSync")
    except Exception as e:
        logger.error(f"Failed to update activity {activity.activity_id} in AppSync: {e}")
        raise


def _save_debug_output(*, activity: Activity, debugger: ActivityDebugger) -> None:
    """Save debug file output.

    Args:
        activity: Activity object to process
        debugger: ActivityDebugger object to save debug output
    """
    try:
        debug_file = debugger.save_debug_file()
        logger.info(f"Debug analysis saved for activity {activity.activity_id}: {debug_file}")
    except Exception as e:
        logger.error(f"Failed to save debug file for activity {activity.activity_id}: {e}")


def _create_debug_response(
    *, activity: Activity, debugger: ActivityDebugger
) -> tuple[bool, str, Any]:
    """Create debug response with accuracy stats.

    Args:
        activity: Activity object to process
        debugger: ActivityDebugger object to save debug output
    """
    try:
        accuracy_stats = debugger.get_activity_accuracy_stats()
        return True, activity.activity_id, accuracy_stats
    except Exception as e:
        logger.error(f"Failed to get accuracy stats for activity {activity.activity_id}: {e}")
        return True, activity.activity_id, None


def _create_error_response(*, activity_id: str, debug: bool) -> tuple[bool, str, Any]:
    """Create standardized error response.

    Args:
        activity_id: Activity ID
        debug: If True, creates detailed debug output file for this activity
    """
    if debug:
        return False, activity_id, None
    return False, activity_id, None
