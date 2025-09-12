#!/usr/bin/env python3
"""Athena staging cleanup script for removing old S3 objects.

This script removes Athena staging objects older than a specified age
from S3 buckets, supporting both Lambda and CLI execution modes.
"""

import argparse
import os
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Annotated, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from pydantic import BaseModel, Field, field_validator


class ExitCode(IntEnum):
    """Exit codes for the cleanup operation."""

    SUCCESS = 0
    FAILURE = 1


class CleanupConfig(BaseModel):
    """Configuration for Athena staging cleanup."""

    bucket: str = Field(..., description="S3 bucket name")
    prefix: str = Field(..., description="S3 key prefix for staging objects")
    age_days: Annotated[int, Field(ge=0)] = 7
    region: str = Field(default="us-east-1", description="AWS region for S3 client")

    @field_validator("bucket")
    def validate_bucket_name(cls, v: str) -> str:
        """Validate S3 bucket name format."""
        if not v or not v.replace("-", "").replace(".", "").isalnum():
            raise ValueError(f"Invalid S3 bucket name: {v}")
        return v

    @field_validator("prefix")
    def validate_prefix(cls, v: str) -> str:
        """Validate S3 prefix format."""
        if not v:
            raise ValueError("S3 prefix cannot be empty")
        return v.rstrip("/")


class S3Object(BaseModel):
    """S3 object metadata."""

    key: str = Field(..., description="S3 object key")
    last_modified: datetime = Field(..., description="Last modification timestamp")

    @field_validator("last_modified")
    def ensure_timezone_aware(cls, v: datetime) -> datetime:
        """Ensure datetime is timezone-aware."""
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class S3CleanupClient:
    """S3 client wrapper for cleanup operations."""

    BATCH_SIZE: Final[int] = 1000

    def __init__(self, *, region: str) -> None:
        """Initialize S3 client.

        Args:
            region: AWS region name.
        """
        self._client = boto3.client("s3", region_name=region)
        self._region = region

    def list_objects_with_prefix(self, *, bucket: str, prefix: str) -> list[S3Object]:
        """List all objects with the specified prefix.

        Args:
            bucket: S3 bucket name.
            prefix: S3 key prefix.

        Returns:
            List of S3 objects with metadata.

        Raises:
            ClientError: If S3 operation fails.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)

        objects: list[S3Object] = []
        for page in page_iterator:
            contents = page.get("Contents", [])
            for obj in contents:
                if "Key" not in obj or "LastModified" not in obj:
                    continue

                objects.append(S3Object(key=obj["Key"], last_modified=obj["LastModified"]))

        return objects

    def delete_objects_batch(self, *, bucket: str, keys: list[str]) -> None:
        """Delete a batch of objects.

        Args:
            bucket: S3 bucket name.
            keys: List of object keys to delete.

        Raises:
            ClientError: If delete operation fails.
        """
        if not keys:
            return

        try:
            self._client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True}
            )
        except (BotoCoreError, ClientError) as e:
            logger.warning(f"Delete batch failed for {len(keys)} objects: {e}")
            raise

    def delete_objects_in_batches(self, *, bucket: str, keys: list[str]) -> None:
        """Delete objects in batches to avoid API limits.

        Args:
            bucket: S3 bucket name.
            keys: List of object keys to delete.
        """
        for idx in range(0, len(keys), self.BATCH_SIZE):
            batch: list[str] = keys[idx : idx + self.BATCH_SIZE]
            self.delete_objects_batch(bucket=bucket, keys=batch)


def _calculate_cutoff_date(*, age_days: int) -> datetime:
    """Calculate cutoff date for object deletion.

    Args:
        age_days: Number of days to look back.

    Returns:
        Cutoff datetime in UTC.
    """
    return datetime.now(UTC) - timedelta(days=age_days)


def _filter_objects_by_age(*, objects: list[S3Object], cutoff_date: datetime) -> list[str]:
    """Filter objects older than cutoff date.

    Args:
        objects: List of S3 objects.
        cutoff_date: Cutoff datetime for deletion.

    Returns:
        List of object keys eligible for deletion.
    """
    return [obj.key for obj in objects if obj.last_modified < cutoff_date]


def _execute_cleanup(*, config: CleanupConfig) -> ExitCode:
    """Execute the cleanup operation.

    Args:
        config: Cleanup configuration.

    Returns:
        Exit code indicating success or failure.
    """
    logger.info(
        f"Starting Athena staging cleanup: "
        f"bucket={config.bucket}, prefix={config.prefix}, "
        f"age_days={config.age_days}, region={config.region}"
    )

    try:
        client: S3CleanupClient = S3CleanupClient(region=config.region)
        cutoff_date = _calculate_cutoff_date(age_days=config.age_days)

        objects = client.list_objects_with_prefix(bucket=config.bucket, prefix=config.prefix)

        delete_keys: list[str] = _filter_objects_by_age(objects=objects, cutoff_date=cutoff_date)

        logger.info(f"Found {len(objects)} total objects, {len(delete_keys)} eligible for deletion")

        if delete_keys:
            client.delete_objects_in_batches(bucket=config.bucket, keys=delete_keys)
            logger.info(f"Successfully deleted {len(delete_keys)} objects")
        else:
            logger.info("No objects found for deletion")

        logger.info("Cleanup completed successfully")
        return ExitCode.SUCCESS

    except (BotoCoreError, ClientError) as e:
        logger.error(f"AWS operation failed: {e}")
        return ExitCode.FAILURE
    except Exception as e:
        logger.error(f"Unexpected error during cleanup: {e}")
        return ExitCode.FAILURE


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda handler for cleanup operation.

    Args:
        event: Lambda event containing cleanup parameters.
        context: Lambda context (unused).

    Returns:
        Lambda response with status code and body.
    """
    try:
        config: CleanupConfig = CleanupConfig(
            bucket=event.get("bucket", ""),
            prefix=event.get("prefix", "athena_staging"),
            age_days=int(event.get("age_days", 7)),
            region=os.getenv("AWS_REGION", "us-east-1"),
        )

        exit_code: ExitCode = _execute_cleanup(config=config)

        return {
            "statusCode": 200 if exit_code == ExitCode.SUCCESS else 500,
            "body": f"Cleanup {'completed' if exit_code == ExitCode.SUCCESS else 'failed'}",
        }

    except ValueError as e:
        logger.error(f"Invalid configuration: {e}")
        return {"statusCode": 400, "body": f"Invalid configuration: {e}"}
    except Exception as e:
        logger.error(f"Lambda handler error: {e}")
        return {"statusCode": 500, "body": f"Internal error: {e}"}


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create command line argument parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Delete Athena staging objects older than N days from S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Clean up objects older than 7 days
  python athena_staging_cleanup.py --bucket my-bucket --prefix athena_staging

  # Clean up objects older than 30 days in specific region
  python athena_staging_cleanup.py \\
    --bucket my-bucket \\
    --prefix athena_staging \\
    --age-days 30 \\
    --region us-west-2
        """,
    )

    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--prefix", required=True, help="S3 key prefix for staging objects")
    parser.add_argument(
        "--age-days",
        type=int,
        default=7,
        help="Delete objects older than this many days (default: 7)",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "us-east-1"),
        help="AWS region for S3 client (default: AWS_REGION env var or us-east-1)",
    )

    return parser


def main() -> int:
    """Main function for CLI execution.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    parser = _create_argument_parser()
    args = parser.parse_args()

    try:
        config: CleanupConfig = CleanupConfig(
            bucket=args.bucket, prefix=args.prefix, age_days=args.age_days, region=args.region
        )

        exit_code: ExitCode = _execute_cleanup(config=config)
        return int(exit_code)

    except ValueError as e:
        logger.error(f"Invalid configuration: {e}")
        return int(ExitCode.FAILURE)
    except Exception as e:
        logger.error(f"Main function error: {e}")
        return int(ExitCode.FAILURE)


if __name__ == "__main__":
    raise SystemExit(main())
