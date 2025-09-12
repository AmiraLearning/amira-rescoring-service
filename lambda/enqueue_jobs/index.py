import asyncio
import json
import os
import time
from datetime import UTC
from typing import Any

import aioboto3
import polars as pl
from pydantic import BaseModel, Field

from infra.athena_client import AthenaClientConfig, ProductionAthenaClient
from infra.sqs_utils import JobMessage, SQSEnqueuer
from src.pipeline.query import build_activity_query


class JobEnqueuer(BaseModel):
    queue_url: str = Field(..., description="SQS queue URL")
    athena_config: AthenaClientConfig = Field(..., description="Athena configuration")

    @classmethod
    def from_env(cls) -> "JobEnqueuer":
        required_vars = ["JOBS_QUEUE_URL", "ATHENA_DATABASE"]
        for var in required_vars:
            if not os.environ.get(var):
                raise ValueError(f"Missing required environment variable: {var}")

        queue_url = os.environ["JOBS_QUEUE_URL"]
        if not queue_url.startswith("https://sqs."):
            raise ValueError("Invalid SQS queue URL format")

        athena_config = AthenaClientConfig(
            database=os.environ["ATHENA_DATABASE"],
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            aws_profile=os.environ.get("AWS_PROFILE", "legacy"),
        )

        return cls(queue_url=queue_url, athena_config=athena_config)


class LambdaResponse(BaseModel):
    status_code: int
    body: str


async def enqueue_activities_from_athena(enqueuer: JobEnqueuer) -> int:
    session = aioboto3.Session()

    athena = ProductionAthenaClient(config=enqueuer.athena_config)
    try:
        override_query = os.environ.get("ATHENA_QUERY")
        if override_query and override_query.strip():
            query_str: str = override_query
        else:
            from datetime import datetime, timedelta

            utc_now: datetime = datetime.now(UTC)
            end_utc: datetime = datetime(utc_now.year, utc_now.month, utc_now.day, tzinfo=UTC)
            start_utc: datetime = end_utc - timedelta(days=1)
            start_str: str = start_utc.strftime("%Y-%m-%d %H:%M:%S")
            end_str: str = end_utc.strftime("%Y-%m-%d %H:%M:%S")

            table = os.environ.get("ATHENA_TABLE", "activity_v")
            act_type = os.environ.get("ACTIVITY_TYPE_FILTER", "letterNamingAndSounds")
            act_status = os.environ.get("ACTIVITY_STATUS_FILTER", "under_review")
            districts_env = os.environ.get("DISTRICTS_FILTER", "955168,1000022968")
            districts = tuple([d.strip() for d in districts_env.split(",") if d.strip()])

            query_str = build_activity_query(
                start_time=start_str,
                end_time=end_str,
                table=table,
                activity_type=act_type,
                status=act_status,
                districts=districts,
            )

        result: pl.DataFrame = await athena.execute_query(query=query_str, return_dataframe=True)
        activity_col: str = "activityid" if "activityid" in result.columns else result.columns[0]
        story_col: str | None = "storyid" if "storyid" in result.columns else None
        student_col: str | None = "studentid" if "studentid" in result.columns else None
        created_col: str | None = "createdat" if "createdat" in result.columns else None
        activity_ids: list[str] = result[activity_col].to_list()
        story_ids: list[str | None] = (
            result[story_col].to_list() if story_col else [None] * len(activity_ids)
        )
        student_ids = result[student_col].to_list() if student_col else [None] * len(activity_ids)
        created_ats = result[created_col].to_list() if created_col else [None] * len(activity_ids)

        async with session.client("sqs") as sqs_client:
            sqs_enqueuer = SQSEnqueuer(client=sqs_client, queue_url=enqueuer.queue_url)

            messages = [
                JobMessage(
                    activity_id=activity_id,
                    story_id=story_id,
                    student_id=student_id,
                    created_at=str(created_at) if created_at is not None else None,
                    source="athena_enqueuer",
                )
                for activity_id, story_id, student_id, created_at in zip(
                    activity_ids, story_ids, student_ids, created_ats
                )
            ]

            return await sqs_enqueuer.enqueue_batch(messages=messages)
    finally:
        await athena.close()
    return 0


async def send_slack_kickoff_notification(jobs_enqueued: int) -> None:
    """Send pipeline kickoff notification to Slack.

    Args:
        jobs_enqueued: Number of jobs enqueued.
    """
    from loguru import logger

    slack_function_name: str | None = os.environ.get("SLACK_NOTIFIER_FUNCTION_NAME")
    if not slack_function_name:
        return  # Slack notifications not configured

    try:
        session = aioboto3.Session()
        async with session.client("lambda") as lambda_client:
            payload: dict[str, Any] = {
                "source": "pipeline_kickoff",
                "jobs_enqueued": jobs_enqueued,
                "timestamp": time.time(),
            }

            await lambda_client.invoke(
                FunctionName=slack_function_name,
                InvocationType="Event",  # Async invocation
                Payload=json.dumps(payload),
            )
    except Exception as e:
        logger.error(f"Failed to send Slack notification: {e}")
        # Don't fail the job enqueuing if Slack notification fails


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for enqueuing jobs from Athena.

    Args:
        event: Lambda event.
        context: Lambda context.

    Returns:
        Lambda response.
    """
    handler_start: float = time.time()

    try:
        enqueuer: JobEnqueuer = JobEnqueuer.from_env()

        count: int = asyncio.run(enqueue_activities_from_athena(enqueuer))
        if count > 0:
            asyncio.run(send_slack_kickoff_notification(count))

        response: LambdaResponse = LambdaResponse(
            status_code=200,
            body=json.dumps({"enqueued": count, "handlerTime": time.time() - handler_start}),
        )

        return response.model_dump()

    except Exception as e:
        return LambdaResponse(
            status_code=500,
            body=json.dumps({"error": str(e), "handlerTime": time.time() - handler_start}),
        ).model_dump()
