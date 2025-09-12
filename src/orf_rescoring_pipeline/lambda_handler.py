"""
Lambda handler for ORF rescoring pipeline.

This module provides a serverless entry point for processing individual
activities in AWS Lambda. Each invocation processes exactly one activity
independently with proper resource management and error handling.
"""

from typing import Any, Final

from amira_pyutils.general.environment import Environment, Environments
from amira_pyutils.services.appsync import AppSync
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from orf_rescoring_pipeline import constants
from orf_rescoring_pipeline.core.processor import process_single_activity
from orf_rescoring_pipeline.models import Activity
from orf_rescoring_pipeline.utils.transcription import (
    DeepgramASRClient,
    KaldiASRClient,
    W2VASRClient,
)

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

    def close_all(self) -> None:
        """Close all ASR client connections."""
        for client_name in ["kaldi", "deepgram", "w2v"]:
            try:
                client = getattr(self, client_name)
                if hasattr(client, "close"):
                    client.close()
            except Exception as cleanup_error:
                logger.warning(f"Failed to close {client_name}: {cleanup_error}")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for ORF rescoring pipeline.

    Args:
        event: Lambda event containing activity_id and configuration
        context: Lambda context (unused)

    Returns:
        HTTP-style response with processing results
    """
    try:
        validated_event = LambdaEvent.model_validate(event)
        result = _process_activity_with_cleanup(event=validated_event)
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


def _process_activity_with_cleanup(*, event: LambdaEvent) -> ProcessingResult:
    """
    Process activity with guaranteed resource cleanup.

    Args:
        event: Validated Lambda event

    Returns:
        Processing result with success status
    """
    asr_clients: ASRClients | None = None

    try:
        env = _initialize_environment(env_name=event.env_name)
        appsync = AppSync(env=env)
        asr_clients = _initialize_asr_clients()

        activity = _load_activity_data(appsync=appsync, activity_id=event.activity_id)

        success = _execute_processing_pipeline(
            activity=activity, appsync=appsync, env=env, asr_clients=asr_clients, debug=event.debug
        )

        return ProcessingResult(activity_id=event.activity_id, success=success, debug=event.debug)
    except Exception as e:
        return ProcessingResult(
            activity_id=event.activity_id, success=False, debug=event.debug, error=str(e)
        )
    finally:
        if asr_clients:
            asr_clients.close_all()


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
        return Environments.find(env_name)
    except Exception as env_error:
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
    activities_data = appsync.get_activity_story_data([activity_id])

    if not activities_data or activity_id not in activities_data:
        raise ValueError(f"Activity '{activity_id}' not found")

    activities = Activity.from_appsync_res([activities_data[activity_id]])
    activity = activities[0] if activities else None
    if activity is None:
        raise ValueError(f"Failed to create Activity from data for {activity_id}")

    model_features = appsync.get_model_features_by_activity_ids([activity_id])
    if activity_id in model_features:
        activity.model_features_from_appsync_res(appsync_model_res=model_features[activity_id])
    else:
        logger.warning(f"No model features found for activity {activity_id}")

    return activity


def _execute_processing_pipeline(
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
    result = process_single_activity(
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

    return result[0] if isinstance(result, tuple) else result


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
