import time
from typing import Any

from pydantic import BaseModel, Field


class JobMessage(BaseModel):
    activity_id: str
    source: str
    # Default timestamp at construction if not provided
    timestamp: int | None = Field(default_factory=lambda: int(time.time()))


class SQSEnqueuer:
    def __init__(self, client: Any, queue_url: str):
        self.client = client
        self.queue_url = queue_url

    async def enqueue_batch(self, messages: list[JobMessage]) -> int:
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
            except Exception:
                pass

        return count
