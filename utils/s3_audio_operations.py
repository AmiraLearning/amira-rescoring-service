"""S3 audio operations utilities.

This module contains S3-specific audio file operations extracted from phrase_slicing.py
for better code organization.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from loguru import logger

from infra.s3_client import ProductionS3Client


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
    try:
        # Use list_objects_batch method from ProductionS3Client
        operations = [(source_bucket, prefix_path)]
        results = await s3_client.list_objects_batch(operations)
        result = results[0] if results else None

        if not result or not result.success:
            logger.warning(f"S3 list failed for {source_bucket}/{prefix_path}")
            return []

        contents: list[dict[str, Any]] = (
            result.data.get("Contents", []) if result.data else []
        )
        return [obj["Key"] for obj in contents if "Key" in obj]

    except Exception as e:
        logger.error(f"Error finding S3 objects in {source_bucket}/{prefix_path}: {e}")
        if retry_limit and retry_limit > 0:
            logger.info(f"Retrying S3 find, {retry_limit} attempts remaining")
            await asyncio.sleep(1)
            return await s3_find(
                source_bucket=source_bucket,
                prefix_path=prefix_path,
                s3_client=s3_client,
                retry_limit=retry_limit - 1,
            )
        raise


def bucket_for(*, stage_source: bool) -> str:
    """Get appropriate S3 bucket based on environment.

    Args:
        stage_source: True for staging, False for production

    Returns:
        S3 bucket name
    """
    from utils.config import S3_SPEECH_ROOT_STAGE, S3_SPEECH_ROOT_PROD

    return S3_SPEECH_ROOT_STAGE if stage_source else S3_SPEECH_ROOT_PROD


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

    bucket = bucket_for(stage_source=stage_source)
    prefix = f"{activity_id}/"

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
    bucket = bucket_for(stage_source=stage_source)
    manifest_key = f"{activity_id}/manifest.json"

    try:
        # Use download_files_batch method from ProductionS3Client
        temp_path = f"/tmp/{manifest_key.replace('/', '_')}"
        operations = [(bucket, manifest_key, temp_path)]
        results = await s3_client.download_files_batch(operations)
        result = results[0] if results else None

        if result and result.success:
            # Read the downloaded file
            temp_file = Path(temp_path)
            if temp_file.exists():
                content = temp_file.read_bytes()
                temp_file.unlink()  # Clean up
                return json.loads(content.decode("utf-8"))

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
    bucket = bucket_for(stage_source=stage_source)
    manifest_key = f"{activity_id}/manifest.json"

    try:
        content = json.dumps(manifest_data, indent=2).encode("utf-8")

        # Use upload_files_batch method from ProductionS3Client
        # First write content to temp file
        temp_file = Path("/tmp") / f"temp_{manifest_key.replace('/', '_')}"
        temp_file.write_bytes(content)

        operations = [(str(temp_file), bucket, manifest_key)]
        results = await s3_client.upload_files_batch(operations)
        result = results[0] if results else None

        # Clean up temp file
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

    bucket = bucket_for(stage_source=stage_source)
    _ensure_parent_dir(path=destination_path)

    # Prepare download operations
    operations = []
    for s3_key in segment_file_names:
        local_file = Path(destination_path) / Path(s3_key).name
        operations.append((bucket, s3_key, str(local_file)))

    # Execute batch download
    try:
        results = await s3_client.download_files_batch(operations)
        success_count = sum(1 for r in results if r.success)

        logger.info(f"Downloaded {success_count}/{len(operations)} segment files")
        return success_count == len(operations)

    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return False
