from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import aioboto3
from loguru import logger
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field, field_validator

from utils.extract_phrases import extract_phrase_slices_tutor_style
from src.pipeline.pipeline import process_single_activity
# from src.pipeline.inference.engine import Wav2Vec2InferenceEngine, W2VConfig


class SQSMessage(BaseModel):
    """SQS message.

    Attributes:
        activityId: The activity ID
        dataset: The dataset name
        replaySuffix: The replay suffix
        timestamp: Message timestamp for replay protection
        source: Message source for validation
    """

    activityId: str
    dataset: str | None = None
    replaySuffix: str | None = None
    timestamp: int | None = None
    source: str | None = None

    @field_validator("activityId")
    @classmethod
    def validate_activity_id(cls, v):
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(f"Invalid activity ID format: {v}")
        if len(v) > 128:
            raise ValueError("Activity ID too long")
        return v

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, v):
        if v and not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(f"Invalid dataset format: {v}")
        return v

    @field_validator("replaySuffix")
    @classmethod
    def validate_replay_suffix(cls, v):
        if v and not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(f"Invalid replay suffix format: {v}")
        return v


# TODO coordinate this with the pipeline config
class WorkerConfig(BaseModel):
    """Worker configuration.

    Attributes:
        queue_url: SQS queue URL for job messages
        results_bucket: S3 bucket for storing results
        model_path: HF model path for Wav2Vec2
        include_confidence: Whether to compute confidence scores
        audio_dir: Local directory for audio processing
        results_prefix: S3 key prefix for results
        visibility_timeout: SQS visibility timeout in seconds
        max_messages: Maximum number of messages to receive
        wait_time: SQS long polling wait time
        aws_region: AWS region for SQS and S3
        max_processing_time: Maximum time to process a single message
        retry_attempts: Maximum retry attempts for failed messages
    """

    queue_url: str = Field(..., description="SQS queue URL for job messages")
    results_bucket: str = Field(..., description="S3 bucket for storing results")
    model_path: str = Field(
        "facebook/wav2vec2-base-960h", description="HF model path for Wav2Vec2"
    )
    include_confidence: bool = Field(
        True, description="Whether to compute confidence scores"
    )
    audio_dir: str = Field(
        "/tmp/audio", description="Local directory for audio processing"
    )
    results_prefix: str = Field("results/", description="S3 key prefix for results")
    visibility_timeout: int = Field(
        900, description="SQS visibility timeout in seconds"
    )
    max_messages: int = Field(5, description="Maximum number of messages to receive")
    wait_time: int = Field(10, description="SQS long polling wait time")
    aws_region: str = Field("us-east-1", description="AWS region for SQS and S3")
    max_processing_time: int = Field(
        600, description="Maximum processing time per message"
    )
    retry_attempts: int = Field(3, description="Maximum retry attempts")

    @field_validator("queue_url")
    @classmethod
    def validate_queue_url(cls, v):
        if not v.startswith("https://sqs.") or not v.endswith(".amazonaws.com"):
            raise ValueError("Invalid SQS queue URL format")
        return v

    @field_validator("results_bucket")
    @classmethod
    def validate_bucket_name(cls, v):
        if not re.match(r"^[a-z0-9.-]{3,63}$", v):
            raise ValueError("Invalid S3 bucket name format")
        return v

    @field_validator("audio_dir")
    @classmethod
    def validate_audio_dir(cls, v):
        # Resolve and validate the path to prevent traversal attacks
        resolved_path = Path(v).resolve()

        # Ensure it's within allowed directories
        allowed_prefixes = [Path("/tmp"), Path("/opt/app"), Path("/var/task")]

        if not any(
            str(resolved_path).startswith(str(prefix)) for prefix in allowed_prefixes
        ):
            raise ValueError(f"Audio directory path not allowed: {resolved_path}")

        # Create directory if it doesn't exist
        resolved_path.mkdir(parents=True, exist_ok=True)

        return str(resolved_path)

    @field_validator("results_prefix")
    @classmethod
    def validate_results_prefix(cls, v):
        if not re.match(r"^[a-zA-Z0-9/_-]*$", v):
            raise ValueError("Invalid results prefix format")
        return v

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """Create configuration from environment variables.

        Returns:
            The worker configuration.
        """
        return cls(
            queue_url=get_required_env("JOBS_QUEUE_URL"),
            results_bucket=get_required_env("RESULTS_BUCKET"),
            model_path=os.getenv("MODEL_PATH", "facebook/wav2vec2-base-960h"),
            include_confidence=os.getenv("INCLUDE_CONFIDENCE", "true").lower()
            == "true",
            audio_dir=os.getenv("AUDIO_DIR", "/tmp/audio"),
            results_prefix=os.getenv("RESULTS_PREFIX", "results/"),
        )


def get_required_env(name: str) -> str:
    """Get a required environment variable or raise an error."""
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", name):
        raise ValueError(f"Invalid environment variable name: {name}")

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    if len(value) > 2048:
        raise ValueError(f"Environment variable {name} too long")

    return value


def extract_audio_phrases(
    *,
    activity_id: str,
    audio_dir: str,
    dataset_name: str | None,
    replay_suffix: str | None,
    s3_client: Any,
) -> None:
    """Extract audio phrases from the activity using tutor-style extraction.

    Args:
        activity_id: The activity ID.
        audio_dir: The audio directory.
        dataset_name: The dataset name.
        replay_suffix: The replay suffix.
        s3_client: The S3 client.
    """
    extract_phrase_slices_tutor_style(
        activity_id=activity_id,
        activity_dir=audio_dir,
        dataset_name=dataset_name,
        replay_suffix=replay_suffix,
        audio_s3_root=None,
        s3=s3_client,
        stage_source=False,
        use_audio_dir_as_activities_root=False,
    )


async def write_results_to_s3(
    *, activity_id: str, results_bucket: str, results_prefix: str, s3_client: Any
) -> None:
    """Write processing results to S3 in Parquet format with success marker."""
    records: list[dict[str, Any]] = [{"activityId": activity_id, "status": "processed"}]
    table: pa.Table = pa.Table.from_pylist(records)

    dt_prefix: str = time.strftime("dt=%Y-%m-%d")
    base_key: str = (
        f"{results_prefix.rstrip('/')}/{dt_prefix}/activity_id={activity_id}"
    )
    data_key: str = f"{base_key}/results.parquet"
    success_key: str = f"{base_key}/_SUCCESS"

    sink: pa.BufferOutputStream = pa.BufferOutputStream()
    pq.write_table(table, sink)
    buf = sink.getvalue().to_pybytes()

    await s3_client.put_object(Bucket=results_bucket, Key=data_key, Body=buf)
    await s3_client.put_object(Bucket=results_bucket, Key=success_key, Body=b"")

# TODO wire up the activity group to the pipeline
async def process_message(
    *, msg: SQSMessage, config: WorkerConfig, s3_client: Any
) -> None:
    """Process a single SQS message containing an activity to analyze.

    Args:
        msg: The SQS message.
        config: The worker configuration.
        s3_client: The S3 client.
    """
    start_time = time.time()

    # Validate message age to prevent replay attacks
    if msg.timestamp:
        message_age = time.time() - msg.timestamp
        if message_age > 3600:  # 1 hour max age
            logger.warning(
                f"Rejecting old message: {msg.activityId} (age: {message_age}s)"
            )
            return

    try:
        process_single_activity(
            activity_id=msg.activityId,
            phrases_input=[],
            config=config,
        )

        processing_time = time.time() - start_time
        logger.info(
            f"Successfully processed {msg.activityId} in {processing_time:.2f}s"
        )

    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(
            f"Failed to process {msg.activityId} after {processing_time:.2f}s: {type(e).__name__}"
        )
        raise


async def handle_sqs_message(
    *, record: dict[str, Any], config: WorkerConfig, sqs_client: Any, s3_client: Any
) -> None:
    """Handle a single SQS message with error handling and visibility timeout management.

    Args:
        record: The SQS message record.
        config: The worker configuration.
        sqs_client: The SQS client.
        s3_client: The S3 client.
    """
    receipt: str = record.get("ReceiptHandle", "")
    message_id: str = record.get("MessageId", "unknown")

    if not receipt:
        logger.error(f"Missing receipt handle for message {message_id}")
        return

    retry_count = 0
    max_retries = config.retry_attempts

    while retry_count <= max_retries:
        try:
            # Parse and validate message
            raw_body = record.get("Body", "")
            if not raw_body:
                logger.error(f"Empty message body for {message_id}")
                break

            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in message {message_id}: {e}")
                break

            # Validate message structure
            if not isinstance(body, dict):
                logger.error(f"Message body is not a dict for {message_id}")
                break

            # Create validated message object
            try:
                msg = SQSMessage(**body)
            except Exception as e:
                logger.error(f"Message validation failed for {message_id}: {e}")
                break

            start_ts = time.time()

            # Process with timeout
            try:
                await asyncio.wait_for(
                    process_message(msg=msg, config=config, s3_client=s3_client),
                    timeout=config.max_processing_time,
                )
            except asyncio.TimeoutError:
                logger.error(f"Processing timeout for message {message_id}")
                raise

            # Extend visibility timeout if needed
            processing_time = time.time() - start_ts
            TIMEOUT_THRESHOLD = config.visibility_timeout * 0.75

            if processing_time > TIMEOUT_THRESHOLD:
                try:
                    await sqs_client.change_message_visibility(
                        QueueUrl=config.queue_url,
                        ReceiptHandle=receipt,
                        VisibilityTimeout=config.visibility_timeout,
                    )
                except Exception as e:
                    logger.warning(f"Failed to extend visibility timeout: {e}")

            # Delete message on success
            await sqs_client.delete_message(
                QueueUrl=config.queue_url, ReceiptHandle=receipt
            )
            logger.info(f"Successfully processed and deleted message {message_id}")
            return

        except Exception as e:
            retry_count += 1
            logger.error(
                f"Failed processing message {message_id} (attempt {retry_count}/{max_retries + 1}): {type(e).__name__}"
            )

            if retry_count <= max_retries:
                # Exponential backoff
                backoff_time = min(2**retry_count, 30)
                await asyncio.sleep(backoff_time)
            else:
                logger.error(f"Exhausted retries for message {message_id}, giving up")
                # Message will become visible again after visibility timeout
                break


async def poll_and_process_messages(
    *, config: WorkerConfig, sqs_client: Any, s3_client: Any
) -> None:
    """Continuously poll SQS queue and process messages with circuit breaker.

    Args:
        config: The worker configuration.
        sqs_client: The SQS client.
        s3_client: The S3 client.
    """
    consecutive_errors = 0
    max_consecutive_errors = 10
    circuit_breaker_timeout = 60  # seconds
    last_success = time.time()

    while True:
        try:
            # Circuit breaker logic
            if consecutive_errors >= max_consecutive_errors:
                if time.time() - last_success < circuit_breaker_timeout:
                    logger.warning(
                        f"Circuit breaker active, sleeping for {circuit_breaker_timeout}s"
                    )
                    await asyncio.sleep(circuit_breaker_timeout)
                    consecutive_errors = 0  # Reset after timeout

            resp = await sqs_client.receive_message(
                QueueUrl=config.queue_url,
                MaxNumberOfMessages=config.max_messages,
                WaitTimeSeconds=config.wait_time,
                VisibilityTimeout=config.visibility_timeout,
            )

            messages = resp.get("Messages", [])

            if messages:
                logger.info(f"Received {len(messages)} messages")

                # Process messages concurrently with limited parallelism
                semaphore = asyncio.Semaphore(config.max_messages)

                async def process_with_semaphore(record):
                    async with semaphore:
                        await handle_sqs_message(
                            record=record,
                            config=config,
                            sqs_client=sqs_client,
                            s3_client=s3_client,
                        )

                await asyncio.gather(
                    *[process_with_semaphore(record) for record in messages],
                    return_exceptions=True,
                )

                consecutive_errors = 0  # Reset on successful batch
                last_success = time.time()
            else:
                # No messages, brief pause
                await asyncio.sleep(1)

        except Exception as e:
            consecutive_errors += 1
            logger.error(
                f"Error in polling loop (consecutive errors: {consecutive_errors}): {type(e).__name__}"
            )

            # Exponential backoff for errors
            backoff_time = min(2 ** min(consecutive_errors, 6), 60)
            await asyncio.sleep(backoff_time)


def main() -> None:
    """Main entry point for the SQS worker."""
    # Configure structured logging
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
        serialize=True,  # JSON output for structured logging
    )

    try:
        config = WorkerConfig.from_env()
        logger.info(
            f"Starting SQS worker with config: queue={config.queue_url}, region={config.aws_region}"
        )

        # Use session for connection pooling
        session = aioboto3.Session()

        async def run_worker():
            async with (
                session.client("sqs", region_name=config.aws_region) as sqs_client,
                session.client("s3", region_name=config.aws_region) as s3_client,
            ):
                await poll_and_process_messages(
                    config=config, sqs_client=sqs_client, s3_client=s3_client
                )

        asyncio.run(run_worker())

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully")
    except Exception as e:
        logger.exception(f"Fatal error in main: {type(e).__name__}")
        sys.exit(1)


if __name__ == "__main__":
    main()
