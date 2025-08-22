import json
import os
import time
from typing import Final, List, Iterable, Dict, Any, Optional

import boto3
from loguru import logger
from pydantic import BaseModel, Field, field_validator

# Constants
QUEUE_URL: Final[str] = os.environ["JOBS_QUEUE_URL"]
BATCH_SIZE: Final[int] = 10
sqs = boto3.client("sqs")

# TODO share types with other handler


class SQSMessage(BaseModel):
    """Message to be sent to SQS queue.

    Attributes:
        activityId: The activity ID to process
        dataset: Optional dataset name
        replaySuffix: Optional replay suffix for results
        timestamp: Unix timestamp for message creation
        source: Source identifier for the message
    """

    activityId: str
    dataset: Optional[str] = None
    replaySuffix: Optional[str] = None
    timestamp: int = Field(default_factory=lambda: int(time.time()))
    source: str = "manual"


class EnqueueRequest(BaseModel):
    """Request model for manual enqueue operation.

    Attributes:
        activity_ids: List of activity IDs to enqueue
        dataset: Optional dataset name to include in messages
        replaySuffix: Optional replay suffix for results
        source: Source identifier for the messages
    """

    activity_ids: List[str]
    dataset: Optional[str] = None
    replaySuffix: Optional[str] = None
    source: str = "manual"

    @field_validator("activity_ids")
    @classmethod
    def validate_activity_ids(cls, v: List[str]) -> List[str]:
        """Validate that activity_ids is not empty."""
        if not v:
            raise ValueError("activity_ids must not be empty")
        return v


def chunk_list(*, items: List[str], size: int) -> Iterable[List[str]]:
    """Split a list into chunks of specified size.

    Args:
        items: List to be chunked
        size: Maximum size of each chunk

    Returns:
        Iterable of list chunks
    """
    for i in range(0, len(items), size):
        yield items[i : i + size]


def create_sqs_entries(
    *,
    activity_ids: List[str],
    timestamp: int,
    dataset: Optional[str] = None,
    replay_suffix: Optional[str] = None,
    source: str = "manual",
) -> List[Dict[str, str]]:
    """Create SQS message batch entries.

    Args:
        activity_ids: List of activity IDs to process
        timestamp: Unix timestamp for message creation and ID generation
        dataset: Optional dataset name
        replay_suffix: Optional replay suffix
        source: Source identifier for the message

    Returns:
        List of SQS batch entry dictionaries
    """
    entries = []

    for idx, activity_id in enumerate(activity_ids):
        message = SQSMessage(
            activityId=activity_id,
            dataset=dataset,
            replaySuffix=replay_suffix,
            timestamp=timestamp,
            source=source,
        )

        entries.append(
            {"Id": f"{timestamp}-{idx}", "MessageBody": message.model_dump_json()}
        )

    return entries


def send_messages_to_sqs(*, entries: List[Dict[str, str]]) -> Dict[str, Any]:
    """Send batch of messages to SQS.

    Args:
        entries: List of SQS message entries

    Returns:
        Dictionary with SQS response information
    """
    response = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries)

    successful = len(response.get("Successful", []))
    failed = response.get("Failed", [])

    return {"successful": successful, "failed": failed}


def parse_request(event: Dict[str, Any]) -> EnqueueRequest:
    """Parse and validate the incoming request.

    Args:
        event: Lambda event object

    Returns:
        Validated EnqueueRequest object
    """
    if "body" in event and isinstance(event["body"], str):
        payload = json.loads(event["body"])
    else:
        payload = event

    return EnqueueRequest(**payload)


def format_response(*, status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Format the Lambda response.

    Args:
        status_code: HTTP status code
        body: Response body dictionary

    Returns:
        Formatted Lambda response
    """
    return {"statusCode": status_code, "body": json.dumps(body)}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda handler for manually enqueuing activities to SQS.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        API Gateway compatible response
    """
    try:
        # Parse and validate request
        request = parse_request(event)

        # Generate timestamp once for all messages
        timestamp = int(time.time())

        # Process activity IDs in batches
        total_sent = 0

        for batch in chunk_list(items=request.activity_ids, size=BATCH_SIZE):
            entries = create_sqs_entries(
                activity_ids=batch,
                timestamp=timestamp,
                dataset=request.dataset,
                replay_suffix=request.replaySuffix,
                source=request.source,
            )

            result = send_messages_to_sqs(entries=entries)
            total_sent += result["successful"]

            # If any messages failed to send, return partial success
            if result["failed"]:
                logger.warning(
                    f"Failed to send {len(result['failed'])} messages: {result['failed']}"
                )
                return format_response(
                    status_code=207,
                    body={"sent": total_sent, "failed": result["failed"]},
                )

        return format_response(status_code=200, body={"sent": total_sent})

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return format_response(status_code=400, body={"error": str(e)})

    except Exception as e:
        logger.exception(f"Unexpected error: {str(e)}")
        return format_response(status_code=500, body={"error": str(e)})
