from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger


def _list_objects(client: Any, *, bucket: str, prefix: str) -> list[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    page_iter = paginator.paginate(Bucket=bucket, Prefix=prefix)
    objects: list[dict[str, Any]] = []
    for page in page_iter:
        contents = page.get("Contents", [])
        for obj in contents:
            if "Key" in obj:
                objects.append(obj)
    return objects


def _delete_batch(client: Any, *, bucket: str, keys: list[str]) -> None:
    if not keys:
        return
    try:
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
        )
    except (BotoCoreError, ClientError) as e:
        logger.warning(f"Delete batch failed: {e}")


def _run_cleanup(*, bucket: str, prefix: str, age_days: int, region: str) -> int:
    logger.info(
        f"Starting Athena staging cleanup: bucket={bucket}, prefix={prefix}, age_days={age_days}"
    )
    try:
        s3 = boto3.client("s3", region_name=region)
        cutoff = datetime.now(UTC) - timedelta(days=max(0, age_days))
        objs = _list_objects(s3, bucket=bucket, prefix=prefix)
        delete_keys: list[str] = []
        for obj in objs:
            last_mod: datetime | None = obj.get("LastModified")
            key: str = obj.get("Key", "")
            if not key:
                continue
            if last_mod and last_mod.tzinfo is None:
                last_mod = last_mod.replace(tzinfo=UTC)
            if last_mod and last_mod < cutoff:
                delete_keys.append(key)

        logger.info(f"Objects eligible for deletion: {len(delete_keys)}")
        # Delete in batches of 1000
        for i in range(0, len(delete_keys), 1000):
            batch = delete_keys[i : i + 1000]
            _delete_batch(s3, bucket=bucket, keys=batch)
        logger.info("Cleanup completed")
        return 0
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Cleanup failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    bucket = event.get("bucket")
    prefix = event.get("prefix", "athena_staging")
    age_days = int(event.get("age_days", 7))
    region = os.getenv("AWS_REGION", "us-east-1")
    if not bucket:
        return {"statusCode": 400, "body": "Missing required 'bucket'"}
    code = _run_cleanup(bucket=bucket, prefix=prefix, age_days=age_days, region=region)
    return {"statusCode": 200 if code == 0 else 500}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete Athena staging objects older than N days from an S3 prefix."
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--prefix", required=True, help="S3 key prefix for staging (folder)")
    parser.add_argument(
        "--age-days",
        type=int,
        default=7,
        help="Delete objects older than this many days (default: 7)",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "us-east-1"),
        help="AWS region for S3 client (default: env AWS_REGION or us-east-1)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return _run_cleanup(
        bucket=args.bucket,
        prefix=args.prefix,
        age_days=args.age_days,
        region=args.region,
    )


if __name__ == "__main__":
    raise SystemExit(main())
