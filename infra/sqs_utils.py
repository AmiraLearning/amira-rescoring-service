import time
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class JobMessage(BaseModel):
    """Job message."""

    activity_id: str
    source: str
    timestamp: int | None = Field(default_factory=lambda: int(time.time()))


class SQSEnqueuer:
    """SQS enqueuer."""

    def __init__(self, client: Any, queue_url: str):
        """Initialize the SQS enqueuer.

        Args:
            client: SQS client.
            queue_url: SQS queue URL.
        """

        self.client = client
        self.queue_url = queue_url

    async def enqueue_batch(self, messages: list[JobMessage]) -> int:
        """Enqueue a batch of messages.

        Args:
            messages: List of messages to enqueue.

        Returns:
            Number of messages enqueued.
        """

        BATCH_SIZE = 10
        count = 0

        for i in range(0, len(messages), BATCH_SIZE):
            batch = messages[i : i + BATCH_SIZE]
            entries = [
                {"Id": str(idx % BATCH_SIZE), "MessageBody": msg.model_dump_json()}
                for idx, msg in enumerate(batch, start=i)
            ]

            try:
                await self.client.send_message_batch(QueueUrl=self.queue_url, Entries=entries)
                count += len(batch)
            except Exception as e:
                logger.error(f"Failed to enqueue batch starting at {i}: {e}")

        return count
