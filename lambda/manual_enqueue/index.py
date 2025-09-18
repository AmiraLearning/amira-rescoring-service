import asyncio
import json
import os
import time
from typing import Any

import aioboto3
from pydantic import BaseModel, field_validator

from infra.sqs_utils import JobMessage, SQSEnqueuer


class ManualEnqueueRequest(BaseModel):
    activity_ids: list[str]
    source: str = "manual_enqueue"

    @field_validator("activity_ids")
    @classmethod
    def validate_activity_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("activity_ids must not be empty")
        return v


class LambdaResponse(BaseModel):
    status_code: int
    body: str


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    handler_start: float = time.time()

    try:
        if "body" in event and isinstance(event["body"], str):
            payload: dict[str, Any] = json.loads(event["body"])
        else:
            payload = event

        request: ManualEnqueueRequest = ManualEnqueueRequest.model_validate(payload)

        messages: list[JobMessage] = [
            JobMessage(activity_id=activity_id, source=request.source)
            for activity_id in request.activity_ids
        ]

        async def enqueue_messages() -> int:
            session = aioboto3.Session()
            async with session.client("sqs") as sqs_client:
                enqueuer: SQSEnqueuer = SQSEnqueuer(
                    client=sqs_client, queue_url=os.environ["JOBS_QUEUE_URL"]
                )
                result = await enqueuer.enqueue_batch(messages)
                return int(result)

        count: int = asyncio.run(enqueue_messages())

        response: LambdaResponse = LambdaResponse(
            status_code=200,
            body=json.dumps(
                {
                    "enqueued": count,
                    "total": len(request.activity_ids),
                    "handlerTime": time.time() - handler_start,
                }
            ),
        )

        return response.model_dump()

    except Exception as e:
        return LambdaResponse(
            status_code=500,
            body=json.dumps({"error": str(e), "handlerTime": time.time() - handler_start}),
        ).model_dump()
