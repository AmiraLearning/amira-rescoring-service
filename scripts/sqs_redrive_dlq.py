from __future__ import annotations

import argparse
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redrive messages from DLQ back to main queue.")
    parser.add_argument("--dlq-url", required=True, help="Source DLQ queue URL")
    parser.add_argument("--dest-url", required=True, help="Destination main queue URL")
    parser.add_argument(
        "--max", type=int, default=100, help="Max messages to redrive (default 100)"
    )
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    return parser.parse_args()


def _redrive_once(sqs: Any, *, dlq_url: str, dest_url: str) -> int:
    resp = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
    messages = resp.get("Messages", [])
    if not messages:
        return 0
    entries = []
    for m in messages:
        entries.append({"Id": m["MessageId"], "MessageBody": m.get("Body", "{}")})
    sqs.send_message_batch(QueueUrl=dest_url, Entries=entries)
    for m in messages:
        sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
    return len(messages)


def main() -> int:
    args = _parse_args()
    try:
        sqs = boto3.client("sqs", region_name=args.region)
        redriven = 0
        while redriven < args.max:
            n = _redrive_once(sqs, dlq_url=args.dlq_url, dest_url=args.dest_url)
            if n == 0:
                break
            redriven += n
        logger.info(f"Redriven messages: {redriven}")
        return 0
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Redrive failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
