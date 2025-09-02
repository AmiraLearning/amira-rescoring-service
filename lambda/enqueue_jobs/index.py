import json
import os
import time
from typing import Any, AsyncIterator

import aioboto3
import polars as pl
from pydantic import BaseModel, Field, field_validator

from infra.athena_client import ProductionAthenaClient, AthenaClientConfig
from infra.sqs_utils import SQSEnqueuer, JobMessage


class JobEnqueuer(BaseModel):
    queue_url: str = Field(..., description="SQS queue URL")
    athena_config: AthenaClientConfig = Field(..., description="Athena configuration")

    @classmethod
    def from_env(cls) -> "JobEnqueuer":
        required_vars = ['JOBS_QUEUE_URL', 'ATHENA_DATABASE']
        for var in required_vars:
            if not os.environ.get(var):
                raise ValueError(f"Missing required environment variable: {var}")
        
        queue_url = os.environ['JOBS_QUEUE_URL']
        if not queue_url.startswith("https://sqs."):
            raise ValueError("Invalid SQS queue URL format")

        athena_config = AthenaClientConfig(
            database=os.environ['ATHENA_DATABASE'],
            aws_region=os.environ.get('AWS_REGION', 'us-east-1'),
            aws_profile=os.environ.get('AWS_PROFILE', 'legacy')
        )
        
        return cls(
            queue_url=queue_url,
            athena_config=athena_config
        )


class LambdaResponse(BaseModel):
    status_code: int
    body: str


async def enqueue_activities_from_athena(enqueuer: JobEnqueuer) -> int:
    session = aioboto3.Session()
    
    athena = ProductionAthenaClient(config=enqueuer.athena_config)
    try:
        result = await athena.execute_query(
            query="SELECT activityid FROM activity_v WHERE status = 'under_review' LIMIT 10",
            return_dataframe=True
        )
        assert isinstance(result, pl.DataFrame)
        activity_ids = result["activityid"].to_list()
        
        async with session.client("sqs") as sqs_client:
            sqs_enqueuer = SQSEnqueuer(
                client=sqs_client,
                queue_url=enqueuer.queue_url
            )
            
            messages = [
                JobMessage(activity_id=activity_id, source="athena_enqueuer")
                for activity_id in activity_ids
            ]
            
            return await sqs_enqueuer.enqueue_batch(messages=messages)
    finally:
        await athena.close()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    handler_start = time.time()
    
    try:
        enqueuer = JobEnqueuer.from_env()
        import asyncio
        count = asyncio.run(enqueue_activities_from_athena(enqueuer))
        
        response = LambdaResponse(
            status_code=200,
            body=json.dumps({
                'enqueued': count,
                'handlerTime': time.time() - handler_start
            })
        )
        
        return response.model_dump()
        
    except Exception as e:
        return LambdaResponse(
            status_code=500,
            body=json.dumps({
                'error': str(e),
                'handlerTime': time.time() - handler_start
            })
        ).model_dump()