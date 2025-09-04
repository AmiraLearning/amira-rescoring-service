#!/usr/bin/env python3
"""Script to redrive messages from DLQ back to main queue.

This script provides a robust way to move messages from a Dead Letter Queue (DLQ)
back to the main processing queue with proper error handling and batch processing.
"""

import argparse
import os
import sys
from typing import Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from pydantic import BaseModel, Field, field_validator

DEFAULT_MAX_MESSAGES: Final[int] = 100
DEFAULT_REGION: Final[str] = "us-east-1"
BATCH_SIZE: Final[int] = 10
WAIT_TIME_SECONDS: Final[int] = 1


class RedriveConfig(BaseModel):
    """Configuration for DLQ redrive operation."""

    dlq_url: str = Field(..., description="Source DLQ queue URL")
    dest_url: str = Field(..., description="Destination main queue URL")
    max_messages: int = Field(
        default=DEFAULT_MAX_MESSAGES, description="Maximum messages to redrive"
    )
    region: str = Field(default=DEFAULT_REGION, description="AWS region")

    @field_validator("dlq_url", "dest_url")
    def validate_queue_url(cls, v: str) -> str:
        """Validate SQS queue URL format."""
        if not v:
            raise ValueError("Queue URL cannot be empty")

        if not v.startswith("https://sqs."):
            raise ValueError(f"Invalid SQS queue URL format: {v}")

        return v

    @field_validator("max_messages")
    def validate_max_messages(cls, v: int) -> int:
        """Validate max messages is positive."""
        if v <= 0:
            raise ValueError("Max messages must be positive")
        return v


class SQSRedriveClient:
    """SQS client wrapper for redrive operations."""

    def __init__(self, *, region: str) -> None:
        """Initialize SQS client.

        Args:
            region: AWS region name.
        """
        self._client = boto3.client("sqs", region_name=region)

    def redrive_batch(self, *, dlq_url: str, dest_url: str) -> int:
        """Redrive a single batch of messages from DLQ to destination queue.

        Args:
            dlq_url: Source DLQ queue URL.
            dest_url: Destination queue URL.

        Returns:
            Number of messages redriven in this batch.

        Raises:
            ClientError: If SQS operations fail.
        """
        messages = self._receive_messages(queue_url=dlq_url)
        if not messages:
            return 0

        self._send_messages_to_destination(messages=messages, dest_url=dest_url)

        self._delete_messages_from_dlq(messages=messages, dlq_url=dlq_url)

        return len(messages)

    def _receive_messages(self, *, queue_url: str) -> list[dict[str, str]]:
        """Receive messages from queue.

        Args:
            queue_url: Queue URL to receive from.

        Returns:
            List of received messages.
        """
        response = self._client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=BATCH_SIZE, WaitTimeSeconds=WAIT_TIME_SECONDS
        )
        return response.get("Messages", [])

    def _send_messages_to_destination(
        self, *, messages: list[dict[str, str]], dest_url: str
    ) -> None:
        """Send messages to destination queue.

        Args:
            messages: Messages to send.
            dest_url: Destination queue URL.
        """
        entries = [
            {"Id": message["MessageId"], "MessageBody": message.get("Body", "{}")}
            for message in messages
        ]

        self._client.send_message_batch(QueueUrl=dest_url, Entries=entries)

    def _delete_messages_from_dlq(self, *, messages: list[dict[str, str]], dlq_url: str) -> None:
        """Delete messages from DLQ after successful redrive.

        Args:
            messages: Messages to delete.
            dlq_url: DLQ queue URL.
        """
        for message in messages:
            self._client.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create command line argument parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Redrive messages from DLQ back to main queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Redrive up to 100 messages (default)
  python sqs_redrive_dlq.py \\
    --dlq-url https://sqs.us-east-1.amazonaws.com/123456789012/my-dlq \\
    --dest-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue

  # Redrive up to 500 messages in us-west-2
  python sqs_redrive_dlq.py \\
    --dlq-url https://sqs.us-west-2.amazonaws.com/123456789012/my-dlq \\
    --dest-url https://sqs.us-west-2.amazonaws.com/123456789012/my-queue \\
    --max 500 \\
    --region us-west-2
        """,
    )

    parser.add_argument("--dlq-url", required=True, help="Source DLQ queue URL")
    parser.add_argument("--dest-url", required=True, help="Destination main queue URL")
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_MESSAGES,
        help=f"Max messages to redrive (default: {DEFAULT_MAX_MESSAGES})",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", DEFAULT_REGION),
        help=f"AWS region (default: {DEFAULT_REGION})",
    )

    return parser


def _execute_redrive_operation(*, config: RedriveConfig) -> int:
    """Execute the redrive operation.

    Args:
        config: Redrive configuration.

    Returns:
        Total number of messages redriven.

    Raises:
        ClientError: If SQS operations fail.
        BotoCoreError: If AWS SDK operations fail.
    """
    client = SQSRedriveClient(region=config.region)
    total_redriven = 0

    logger.info(
        f"Starting redrive operation: {config.dlq_url} -> {config.dest_url} "
        f"(max: {config.max_messages})"
    )

    while total_redriven < config.max_messages:
        batch_count = client.redrive_batch(dlq_url=config.dlq_url, dest_url=config.dest_url)

        if batch_count == 0:
            logger.info("No more messages available in DLQ")
            break

        total_redriven += batch_count
        logger.info(f"Redriven batch: {batch_count} messages (total: {total_redriven})")

    return total_redriven


def main() -> int:
    """Main function.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    parser = _create_argument_parser()
    args = parser.parse_args()

    try:
        config = RedriveConfig(
            dlq_url=args.dlq_url, dest_url=args.dest_url, max_messages=args.max, region=args.region
        )

        total_redriven = _execute_redrive_operation(config=config)

        logger.info(f"Redrive operation completed successfully: {total_redriven} messages")
        return 0

    except (BotoCoreError, ClientError) as e:
        logger.error(f"AWS operation failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
