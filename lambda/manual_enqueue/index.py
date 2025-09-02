import json
import os
import time
import asyncio
from typing import Any

from pydantic import BaseModel, field_validator

from infra.sqs_utils import SQSEnqueuer, JobMessage


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
    handler_start = time.time()

    try:
        if "body" in event and isinstance(event["body"], str):
            payload = json.loads(event["body"])
        else:
            payload = event

        request = ManualEnqueueRequest.model_validate(payload)

        messages = [
            JobMessage(activity_id=activity_id, source=request.source)
            for activity_id in request.activity_ids
        ]

        import aioboto3

        async def enqueue_messages():
            session = aioboto3.Session()
            async with session.client("sqs") as sqs_client:
                enqueuer = SQSEnqueuer(
                    client=sqs_client, queue_url=os.environ["JOBS_QUEUE_URL"]
                )
                return await enqueuer.enqueue_batch(messages)

        count = asyncio.run(enqueue_messages())

        response = LambdaResponse(
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
            body=json.dumps(
                {"error": str(e), "handlerTime": time.time() - handler_start}
            ),
        ).model_dump()
