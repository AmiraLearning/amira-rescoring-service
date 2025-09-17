"""S3 audio operations utilities.

This module contains S3-specific audio file operations extracted from phrase_slicing.py
for better code organization.
"""

import json
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel
from tenacity import AsyncRetrying, stop_after_attempt, wait_random_exponential

from infra.s3_client import (
    ProductionS3Client,
    S3DownloadRequest,
    S3ListRequest,
    S3OperationResult,
    S3UploadRequest,
)


class S3Object(BaseModel):
    """S3 object metadata."""

    key: str
    size: int
    last_modified: str


async def s3_find(
    *,
    source_bucket: str,
    prefix_path: str,
    s3_client: ProductionS3Client,
    retry_limit: int | None = None,
) -> list[str]:
    """Find S3 objects matching a prefix.

    Args:
        source_bucket: S3 bucket name
        prefix_path: S3 key prefix to search for
        s3_client: S3 client instance
        retry_limit: Maximum retry attempts

    Returns:
        List of S3 object keys matching the prefix

    Raises:
        Exception: If S3 operation fails after retries
    """
    attempts = max(1, int(retry_limit or 1))
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=0.2, max=5.0),
        reraise=True,
    ):
        with attempt:
            operations: list[S3ListRequest] = [
                S3ListRequest(bucket=source_bucket, prefix=prefix_path)
            ]
            results = await s3_client.list_objects_batch(operations)
            result = results[0] if results else None

            if not result or not result.success:
                raise RuntimeError(
                    f"S3 list failed for {source_bucket}/{prefix_path}: {getattr(result, 'error', 'unknown')}"
                )

            objects: list[dict[str, Any]] = result.data.get("objects", []) if result.data else []
            return [
                str(obj.get("Key"))
                for obj in objects
                if isinstance(obj, dict) and obj.get("Key") is not None
            ]
    return []


def bucket_for(*, stage_source: bool) -> str:
    """Get appropriate S3 bucket based on environment.

    Args:
        stage_source: True for staging, False for production (legacy mapping)

    Returns:
        S3 bucket name
    """
    import os

    from utils.config import (
        S3_SPEECH_ROOT_DEV2,
        S3_SPEECH_ROOT_LEGACY_PROD,
        S3_SPEECH_ROOT_PROD,
        S3_SPEECH_ROOT_STAGE,
    )

    # Allow explicit bucket override
    bucket_override: str | None = os.getenv("AUDIO_BUCKET")
    if bucket_override:
        return bucket_override

    # Environment-based bucket selection
    audio_env = os.getenv("AUDIO_ENV", "legacy")

    if audio_env == "prod":
        return S3_SPEECH_ROOT_PROD  # amira-speech-stream-prod (us-east-2)
    elif audio_env == "stage":
        return S3_SPEECH_ROOT_STAGE  # amira-speech-stream-stage (us-east-1)
    elif audio_env == "dev2":
        return S3_SPEECH_ROOT_DEV2  # amira-speech-stream-dev2 (us-east-1)
    else:  # legacy or unset
        # Preserve legacy stage_source behavior for backward compatibility
        return S3_SPEECH_ROOT_STAGE if stage_source else S3_SPEECH_ROOT_LEGACY_PROD


def _ensure_parent_dir(*, path: str) -> None:
    """Ensure parent directory exists for a file path.

    Args:
        path: File path to check
    """
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)


async def get_segment_file_names(
    *,
    activity_id: str | None = None,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> list[str]:
    """Get segment file names from S3 for an activity.

    Args:
        activity_id: Activity identifier
        s3_client: S3 client instance
        stage_source: Use staging bucket if True

    Returns:
        List of segment file names
    """
    if not activity_id:
        return []

    bucket: str = bucket_for(stage_source=stage_source)
    prefix: str = f"{activity_id}/"

    try:
        return await s3_find(
            source_bucket=bucket, prefix_path=prefix, s3_client=s3_client, retry_limit=2
        )
    except Exception as e:
        logger.error(f"Failed to get segment files for {activity_id}: {e}")
        return []


async def load_activity_manifest(
    *,
    activity_id: str,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> dict[str, Any] | None:
    """Load activity manifest from S3.

    Args:
        activity_id: Activity identifier
        s3_client: S3 client instance
        stage_source: Use staging bucket if True

    Returns:
        Manifest data or None if not found
    """
    bucket: str = bucket_for(stage_source=stage_source)
    manifest_key: str = f"{activity_id}/manifest.json"

    try:
        temp_path: str = f"/tmp/{manifest_key.replace('/', '_')}"
        operations: list[S3DownloadRequest] = [
            S3DownloadRequest(bucket=bucket, key=manifest_key, local_path=temp_path)
        ]
        results: list[S3OperationResult] = await s3_client.download_files_batch(operations)
        result: S3OperationResult | None = results[0] if results else None

        if result and result.success:
            temp_file: Path = Path(temp_path)
            if temp_file.exists():
                content: bytes = temp_file.read_bytes()
                temp_file.unlink()
                data = json.loads(content.decode("utf-8"))
                return data if isinstance(data, dict) else None

    except Exception as e:
        logger.debug(f"No manifest found for {activity_id}: {e}")

    return None


async def write_activity_manifest(
    *,
    activity_id: str,
    manifest_data: dict[str, Any],
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> bool:
    """Write activity manifest to S3.

    Args:
        activity_id: Activity identifier
        manifest_data: Manifest data to write
        s3_client: S3 client instance
        stage_source: Use staging bucket if True

    Returns:
        True if successful, False otherwise
    """
    bucket: str = bucket_for(stage_source=stage_source)
    manifest_key: str = f"{activity_id}/manifest.json"

    try:
        content: bytes = json.dumps(manifest_data, indent=2).encode("utf-8")

        temp_file: Path = Path("/tmp") / f"temp_{manifest_key.replace('/', '_')}"
        temp_file.write_bytes(content)

        operations: list[S3UploadRequest] = [
            S3UploadRequest(local_path=str(temp_file), bucket=bucket, key=manifest_key)
        ]
        results: list[S3OperationResult] = await s3_client.upload_files_batch(operations)
        result: S3OperationResult | None = results[0] if results else None

        if temp_file.exists():
            temp_file.unlink()

        return result.success if result else False

    except Exception as e:
        logger.error(f"Failed to write manifest for {activity_id}: {e}")
        return False


async def download_segment_files(
    *,
    segment_file_names: list[str],
    destination_path: str,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> bool:
    """Download segment files from S3 to local directory.

    Args:
        segment_file_names: List of S3 keys to download
        destination_path: Local directory path
        s3_client: S3 client instance
        stage_source: Use staging bucket if True

    Returns:
        True if all downloads successful, False otherwise
    """
    if not segment_file_names:
        logger.warning("No segment files to download")
        return False

    bucket: str = bucket_for(stage_source=stage_source)
    _ensure_parent_dir(path=destination_path)

    operations: list[S3DownloadRequest] = []
    for s3_key in segment_file_names:
        local_file: Path = Path(destination_path) / Path(s3_key).name
        operations.append(S3DownloadRequest(bucket=bucket, key=s3_key, local_path=str(local_file)))

    try:
        results: list[S3OperationResult] = await s3_client.download_files_batch(operations)
        success_count: int = sum(1 for r in results if r.success)

        logger.info(f"Downloaded {success_count}/{len(operations)} segment files")
        return success_count == len(operations)

    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return False
