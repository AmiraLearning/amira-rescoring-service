"""
Lambda handler for ORF rescoring pipeline.

This module provides a serverless entry point for processing individual
activities in AWS Lambda. Each invocation processes exactly one activity
independently with proper resource management and error handling.
"""

from typing import TYPE_CHECKING, Any, Final

from amira_pyutils.logging import get_logger

logger = get_logger(__name__)
from pydantic import BaseModel, Field, ValidationError

from orf_rescoring_pipeline import constants
from orf_rescoring_pipeline.core.processor import process_single_activity
from orf_rescoring_pipeline.models import Activity
from orf_rescoring_pipeline.utils.transcription import (
    DeepgramASRClient,
    KaldiASRClient,
    W2VASRClient,
)

if TYPE_CHECKING:  # pragma: no cover - types only
    from amira_pyutils.environment import Environment
    from amira_pyutils.appsync import AppSync

from amira_pyutils.appsync import AppSync
from amira_pyutils.environment import Environment, Environments

DEFAULT_ENV_NAME: Final[str] = "prod2"


class LambdaEvent(BaseModel):
    """Lambda event payload validation."""

    activity_id: str = Field(..., min_length=1, description="Activity ID to process")
    env_name: str = Field(default=DEFAULT_ENV_NAME, description="Environment name")
    debug: bool = Field(default=False, description="Enable debug mode")


class LambdaResponse(BaseModel):
    """Lambda response payload."""

    statusCode: int
    body: str


class ProcessingResult(BaseModel):
    """Processing result payload."""

    activity_id: str
    success: bool
    debug: bool
    error: str | None = None


class ASRClients(BaseModel):
    """ASR client collection with resource management."""

    deepgram: DeepgramASRClient
    kaldi: KaldiASRClient
    w2v: W2VASRClient

    class Config:
        arbitrary_types_allowed = True

    async def close_all(self) -> None:
        """Close all ASR client connections."""
        for client_name in ["kaldi", "deepgram", "w2v"]:
            try:
                client = getattr(self, client_name)
                if hasattr(client, "close"):
                    if client_name in ["kaldi", "w2v"]:
                        await client.close()
                    else:
                        client.close()
            except Exception as cleanup_error:
                logger.warning(f"Failed to close {client_name}: {cleanup_error}")


async def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for ORF rescoring pipeline.

    Args:
        event: Lambda event containing activity_id and configuration
        context: Lambda context (unused)

    Returns:
        HTTP-style response with processing results
    """
    logger.info(f"Received event in lambda_handler: {event}")
    try:
        logger.debug(f"Validating event: {event}")
        validated_event = LambdaEvent.model_validate(event)
        logger.debug(f"Validated event: {validated_event}")
        result = await _process_activity_with_cleanup(event=validated_event)
        return _create_success_response(result=result)

    except ValidationError as validation_error:
        logger.error(f"Invalid event payload: {validation_error}")
        return _create_error_response(
            status_code=400,
            error="Invalid event payload",
            activity_id=event.get("activity_id", "unknown"),
        )

    except Exception as processing_error:
        logger.error(f"Lambda execution failed: {processing_error}", exc_info=True)
        return _create_error_response(
            status_code=500,
            error=str(processing_error),
            activity_id=event.get("activity_id", "unknown"),
        )


async def _process_activity_with_cleanup(*, event: LambdaEvent) -> ProcessingResult:
    """
    Process activity with guaranteed resource cleanup.

    Args:
        event: Validated Lambda event

    Returns:
        Processing result with success status
    """
    logger.info(f"Processing activity with cleanup: {event}")
    logger.debug(f"Event: {event}")
    asr_clients: ASRClients | None = None
    try:
        logger.debug(f"Initializing environment: {event.env_name}")
        env = _initialize_environment(env_name=event.env_name)
        logger.debug(f"Environment initialized: {env}")
        appsync = AppSync(env=env)
        asr_clients = _initialize_asr_clients()

        logger.debug(f"Loading activity data for {event.activity_id}")
        activity = _load_activity_data(appsync=appsync, activity_id=event.activity_id)

        success = await _execute_processing_pipeline(
            activity=activity, appsync=appsync, env=env, asr_clients=asr_clients, debug=event.debug
        )

        return ProcessingResult(activity_id=event.activity_id, success=success, debug=event.debug)
    except Exception as e:
        logger.error(f"Error processing activity: {e}", exc_info=True)
        return ProcessingResult(
            activity_id=event.activity_id, success=False, debug=event.debug, error=str(e)
        )
    finally:
        if asr_clients:
            await asr_clients.close_all()


def _initialize_environment(*, env_name: str) -> Environment:
    """
    Initialize environment configuration.

    Args:
        env_name: Environment name to load

    Returns:
        Configured environment instance

    Raises:
        ValueError: If environment name is invalid
    """
    try:
        logger.debug(f"Finding environment: {env_name}")
        env = Environments.find(name=env_name)
        if env is None:
            raise ValueError(f"Environment '{env_name}' not found")
        return env
    except Exception as env_error:
        logger.error(f"Invalid environment '{env_name}': {env_error}")
        raise ValueError(f"Invalid environment '{env_name}': {env_error}") from env_error


def _initialize_asr_clients() -> ASRClients:
    """
    Initialize all ASR clients with proper configuration.

    Returns:
        Configured ASR clients collection

    Raises:
        RuntimeError: If client initialization fails
    """
    try:
        return ASRClients(
            deepgram=DeepgramASRClient(api_key=constants.DEEPGRAM_API_KEY),
            kaldi=KaldiASRClient(),
            w2v=W2VASRClient(),
        )
    except Exception as client_error:
        raise RuntimeError(f"Failed to initialize ASR clients: {client_error}") from client_error


def _get_activity_story_data(*, appsync: AppSync, activity_id: str) -> list[dict[str, Any]]:
    """
    Get activity story data from AppSync.
    """
    logger.debug(f"Getting activity story data for {activity_id}")
    results = appsync.get_activity_details(
        activity_ids=[activity_id],
        fields=[
            "activityId",
            "storyId",
            "story.chapters.phrases",
            "errors",
            "story.tags",
        ],
        show_progress=True,
    )
    logger.debug(f"Results: {results}")
    return results


def _get_model_features(*, appsync: AppSync, activity_id: str) -> list[dict[str, Any]]:
    """Fetch model features from AppSync for the given activity.

    Args:
        appsync: AppSync client for data retrieval
        activity_id: Activity ID to load

    Returns:
        List of model features
    """
    features_query = appsync.get_custom_activity_features(
        feature_list=[
            "model",
            "phraseIndex",
            "Kaldi_match",
            "W2V_match",
            "we_dist",
            "correct_confidences",
        ]
    )
    logger.debug(f"Getting model features for {activity_id}")
    logger.debug(f"Features query: {features_query}")
    results = features_query(activity_id=activity_id)
    logger.debug(f"Results: {results}")
    return results


def _load_activity_data(*, appsync: AppSync, activity_id: str) -> Activity:
    """
    Load activity with story data and model features.

    Args:
        appsync: AppSync client for data retrieval
        activity_id: Activity ID to load

    Returns:
        Fully loaded activity instance

    Raises:
        ValueError: If activity not found or invalid
    """
    activities_data = _get_activity_story_data(appsync=appsync, activity_id=activity_id)
    activity = Activity.from_appsync_res([activities_data])

    model_features = _get_model_features(appsync=appsync, activity_id=activity_id)
    logger.debug(f"Model features: {model_features}")
    activity.model_features_from_appsync_res(appsync_model_res=model_features)
    logger.debug(f"leaving _load_activity_data")
    return activity


async def _execute_processing_pipeline(
    *, activity: Activity, appsync: AppSync, env: Environment, asr_clients: ASRClients, debug: bool
) -> bool:
    """
    Execute the core processing pipeline.

    Args:
        activity: Activity to process
        appsync: AppSync client
        env: Environment configuration
        asr_clients: ASR clients collection
        debug: Debug mode flag

    Returns:
        True if processing succeeded, False otherwise
    """
    result = await process_single_activity(
        activity=activity,
        appsync=appsync,
        env=env,
        deepgram=asr_clients.deepgram,
        kaldi=asr_clients.kaldi,
        w2v=asr_clients.w2v,
        debug=debug,
        save_files=False,
        model_features_cache=None,
    )

    from typing import cast as _cast

    if isinstance(result, tuple):
        return bool(result[0])
    return bool(_cast(bool, result))


def _create_success_response(*, result: ProcessingResult) -> dict[str, Any]:
    """
    Create successful Lambda response.

    Args:
        result: Processing result data

    Returns:
        HTTP-style success response
    """
    response = LambdaResponse(statusCode=200, body=result.model_dump_json())
    return response.model_dump()


def _create_error_response(*, status_code: int, error: str, activity_id: str) -> dict[str, Any]:
    """
    Create error Lambda response.

    Args:
        status_code: HTTP status code
        error: Error message
        activity_id: Activity ID for context

    Returns:
        HTTP-style error response
    """
    error_result = ProcessingResult(
        activity_id=activity_id, success=False, debug=False, error=error
    )

    response = LambdaResponse(statusCode=status_code, body=error_result.model_dump_json())
    return response.model_dump()
