import json
import os
import re
import time
from enum import StrEnum
from typing import Any, Final, AsyncIterator, Iterator

import aioboto3
from loguru import logger
from pydantic import BaseModel, Field, field_validator

DEFAULT_MAX_ATTEMPTS: Final[int] = 60
DEFAULT_POLL_INTERVAL_SECONDS: Final[int] = 2
MAX_QUERY_LENGTH: Final[int] = 10000

DANGEROUS_KEYWORDS: Final[list[str]] = [
    "DROP",
    "DELETE",
    "INSERT",
    "UPDATE",
    "CREATE",
    "ALTER",
    "EXEC",
    "EXECUTE",
]

QUERY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^SELECT\s+[\w\s,.'_-]+FROM\s+[\w._-]+(?:\s+WHERE\s+[\w\s=<>!'_-]+)?(?:\s+ORDER\s+BY\s+[\w\s,._-]+)?(?:\s+LIMIT\s+\d+)?$"
)


class AthenaConfig(BaseModel):
    """Configuration for Athena query execution."""

    database: str = Field(..., description="Athena database name")
    output_location: str | None = Field(
        None, description="S3 location for query results"
    )
    query: str = Field(
        "SELECT 'sample-activity' AS activity_id",
        description="SQL query to retrieve activity IDs",
    )

    @field_validator("database")
    def validate_database(cls, v):
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", v):
            raise ValueError("Database name contains invalid characters")
        return v

    @field_validator("query")
    def validate_query(cls, v):
        if len(v) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query too long (max {MAX_QUERY_LENGTH} characters)")

        normalized: str = re.sub(r"--.*$", "", v, flags=re.MULTILINE)
        normalized: str = re.sub(r"/\*.*?\*/", "", normalized, flags=re.DOTALL)
        normalized: str = re.sub(r"\s+", " ", normalized).strip().upper()

        if not QUERY_PATTERN.match(normalized):
            raise ValueError("Query contains potentially unsafe SQL constructs")

        for keyword in DANGEROUS_KEYWORDS:
            if keyword in normalized:
                raise ValueError(f"Query contains forbidden keyword: {keyword}")

        return v

    @field_validator("output_location")
    def validate_output_location(cls, v):
        if v and not v.startswith("s3://"):
            raise ValueError("Output location must be a valid S3 URI")
        return v


class JobEnqueuer(BaseModel):
    """Service to query Athena and enqueue jobs to SQS."""

    queue_url: str = Field(..., description="SQS queue URL for job messages")
    athena_config: AthenaConfig = Field(..., description="Athena query configuration")

    @classmethod
    def from_env(cls) -> "JobEnqueuer":
        """Create configuration from environment variables."""
        try:
            queue_url = get_required_env(name="JOBS_QUEUE_URL")
            if not queue_url.startswith("https://sqs."):
                raise ValueError("Invalid SQS queue URL format")

            database = os.environ.get("ATHENA_DATABASE", "default")
            output_location = os.environ.get("ATHENA_OUTPUT")
            query = os.environ.get(
                "ATHENA_QUERY", "SELECT 'sample-activity' AS activity_id"
            )

            return cls(
                queue_url=queue_url,
                athena_config=AthenaConfig(
                    database=database,
                    output_location=output_location,
                    query=query,
                ),
            )
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            raise


def get_required_env(*, name: str) -> str:
    """Get a required environment variable or raise an error.

    Args:
        name: Name of the environment variable. Must be a single string.

    Returns:
        str: Value of the environment variable.
    """
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", name):
        raise ValueError(f"Invalid environment variable name: {name}")

    value: str = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    if len(value) > 2048:
        raise ValueError(f"Environment variable {name} too long")

    return value


async def execute_athena_query(*, athena_client: Any, config: AthenaConfig) -> str:
    """Execute Athena query and return query execution ID."""
    result_config = (
        {"OutputLocation": config.output_location} if config.output_location else {}
    )

    response: dict[str, Any] = await athena_client.start_query_execution(
        QueryString=config.query,
        QueryExecutionContext={"Database": config.database},
        ResultConfiguration=result_config,
    )
    return response["QueryExecutionId"]


class AthenaTerminalStates(StrEnum):
    """Terminal states for Athena query execution."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


async def wait_for_query_completion(
    *, athena_client: Any, query_id: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> str:
    """Wait for Athena query to complete and return final status.

    Args:
        athena_client: Athena client.
        query_id: Query ID.
        max_attempts: Maximum number of attempts to poll for query completion.

    Returns:
        str: Final status of the query.
    """
    import asyncio

    for attempt in range(max_attempts):
        try:
            response = await athena_client.get_query_execution(
                QueryExecutionId=query_id
            )
            status: str = response["QueryExecution"]["Status"]["State"]

            if status in AthenaTerminalStates:
                return status

            backoff_time = min(
                DEFAULT_POLL_INTERVAL_SECONDS * (2 ** (attempt // 10)), 30
            )
            await asyncio.sleep(backoff_time)

        except Exception as e:
            logger.warning(f"Error checking query status (attempt {attempt + 1}): {e}")
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(DEFAULT_POLL_INTERVAL_SECONDS)

    return "TIMEOUT"


async def fetch_query_results(
    *, athena_client: Any, query_id: str
) -> AsyncIterator[str]:
    """Fetch all activity IDs from query results, handling pagination.

    This function fetches all activity IDs from the query results, handling pagination.
    It yields each activity ID as it is found.

    Args:
        athena_client: Athena client.
        query_id: Query ID.

    Returns:
        AsyncIterator[str]: Iterator of activity IDs.
    """
    next_token: str | None = None
    first_page: bool = True

    while True:
        kwargs: dict[str, Any] = {"QueryExecutionId": query_id}
        if next_token:
            kwargs["NextToken"] = next_token

        page: dict[str, Any] = await athena_client.get_query_results(**kwargs)
        rows: list[dict[str, Any]] = page.get("ResultSet", {}).get("Rows", [])

        if first_page and rows:
            rows = rows[1:]
            first_page = False

        for row in rows:
            values: list[str] = [col.get("VarCharValue") for col in row.get("Data", [])]
            if values:
                yield values[0]

        next_token = page.get("NextToken")
        if not next_token:
            break


async def enqueue_activities_to_sqs(
    *, sqs_client: Any, queue_url: str, activity_ids: AsyncIterator[str]
) -> int:
    """Enqueue activity IDs to SQS in batches with rate limiting.

    Args:
        sqs_client: SQS client.
        queue_url: SQS queue URL.
        activity_ids: Iterator of activity IDs.

    Returns:
        int: Number of activities enqueued.
    """
    import asyncio

    BATCH_SIZE: Final[int] = 10
    MAX_ACTIVITIES: Final[int] = 10000
    RATE_LIMIT_DELAY: Final[float] = 0.1

    count: int = 0
    batch: list[dict[str, str]] = []
    failed_batches: list[list[dict[str, str]]] = []

    async for activity_id in activity_ids:
        if count >= MAX_ACTIVITIES:
            logger.warning(f"Reached maximum activity limit ({MAX_ACTIVITIES})")
            break

        if not re.match(r"^[a-zA-Z0-9_-]+$", activity_id):
            logger.warning(f"Skipping invalid activity ID: {activity_id}")
            continue

        batch.append(
            {
                "Id": str(count % BATCH_SIZE),
                "MessageBody": json.dumps(
                    {
                        "activityId": activity_id,
                        "timestamp": int(time.time()),
                        "source": "athena_job_enqueuer",
                    }
                ),
            }
        )

        if len(batch) == BATCH_SIZE:
            try:
                await sqs_client.send_message_batch(QueueUrl=queue_url, Entries=batch)
                await asyncio.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error(f"Failed to send batch: {e}")
                failed_batches.append(batch.copy())

            batch.clear()
        count += 1

    if batch:
        try:
            await sqs_client.send_message_batch(QueueUrl=queue_url, Entries=batch)
        except Exception as e:
            logger.error(f"Failed to send final batch: {e}")
            failed_batches.append(batch)

    for failed_batch in failed_batches:
        try:
            await sqs_client.send_message_batch(
                QueueUrl=queue_url, Entries=failed_batch
            )
            logger.info(f"Retried failed batch with {len(failed_batch)} messages")
        except Exception as e:
            logger.error(f"Retry failed for batch: {e}")

    return count


async def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler to query Athena and enqueue jobs to SQS.

    Args:
        event: Lambda event.
        context: Lambda context.

    Returns:
        dict: Lambda response.
    """
    try:
        enqueuer: JobEnqueuer = JobEnqueuer.from_env()

        athena_client: Any = aioboto3.client("athena")
        sqs_client: Any = aioboto3.client("sqs")

        query_id: str = await execute_athena_query(
            athena_client=athena_client, config=enqueuer.athena_config
        )

        status: str = await wait_for_query_completion(
            athena_client=athena_client,
            query_id=query_id,
        )

        if status != "SUCCEEDED":
            logger.error(f"Athena query {query_id} failed with status: {status}")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": f"Athena query {query_id} {status}"}),
            }

        activity_ids: AsyncIterator[str] = fetch_query_results(
            athena_client=athena_client, query_id=query_id
        )

        count: int = await enqueue_activities_to_sqs(
            sqs_client=sqs_client,
            queue_url=enqueuer.queue_url,
            activity_ids=activity_ids,
        )

        logger.info(f"Successfully enqueued {count} activities")
        return {"statusCode": 200, "body": json.dumps({"enqueued": count})}

    except Exception as e:
        logger.exception(f"Error enqueueing jobs: {type(e).__name__}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"error": "Internal server error", "timestamp": int(time.time())}
            ),
        }
