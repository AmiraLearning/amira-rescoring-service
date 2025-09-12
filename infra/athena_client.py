"""Production-ready Athena client with async support and comprehensive error handling.

This module provides a robust AWS Athena client implementation following
Expert Python Engineering Principles for Production Systems.
"""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final
from urllib.parse import urlparse

import aioboto3
import polars as pl
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from pydantic import BaseModel, Field

from infra.s3_client import (
    HighPerformanceS3Config,
    ProductionS3Client,
    S3DownloadRequest,
    S3ListRequest,
    S3OperationResult,
)


class AthenaClientError(Exception):
    """Exception raised for errors in the Athena client."""


class AthenaQueryError(Exception):
    """Exception raised for errors in the Athena query."""


class AthenaTimeoutError(Exception):
    """Exception raised for timeout errors in the Athena client."""


DEFAULT_AWS_REGION: Final[str] = "us-east-1"
DEFAULT_DATABASE: Final[str] = "amira-activity-datalake"
DEFAULT_S3_BUCKET: Final[str] = "amira-data-science"
DEFAULT_STAGING_DIR: Final[str] = "athena_staging"
DEFAULT_MAX_EXECUTION_CHECKS: Final[int] = 900
DEFAULT_SLEEP_TIME: Final[int] = 1
DEFAULT_QUERY_TIMEOUT: Final[int] = 3600
NUM_QUERIES_TO_PREVIEW: Final[int] = 200


class QueryState(StrEnum):
    """Athena query execution states."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AthenaResponseKeys(StrEnum):
    """Keys used in Athena API responses."""

    QUERY_EXECUTION_ID = "QueryExecutionId"
    QUERY_EXECUTION = "QueryExecution"
    STATUS = "Status"
    STATE = "State"
    STATE_CHANGE_REASON = "StateChangeReason"
    RESULT_CONFIGURATION = "ResultConfiguration"
    OUTPUT_LOCATION = "OutputLocation"
    STATISTICS = "Statistics"
    ENGINE_EXECUTION_TIME = "EngineExecutionTimeInMillis"
    DATA_SCANNED_BYTES = "DataScannedInBytes"
    QUERY = "Query"
    QUERY_EXECUTION_CONTEXT = "QueryExecutionContext"
    DATABASE = "Database"


class S3Keys(StrEnum):
    """Keys used in S3 responses."""

    OBJECTS = "objects"
    KEY = "Key"


@dataclass(frozen=True)
class QueryResult:
    """Result of an Athena query execution."""

    execution_id: str
    state: QueryState
    s3_output_location: str | None = None
    filename: str | None = None
    error_message: str | None = None
    execution_time_ms: float | None = None
    data_scanned_bytes: int | None = None


@dataclass(frozen=True)
class QueryExecution:
    """Athena query execution details."""

    query: str
    database: str
    execution_id: str
    state: QueryState
    s3_output_location: str | None = None
    error_message: str | None = None
    statistics: dict[str, Any] | None = None


class AthenaClientConfig(BaseModel):
    """Configuration for Athena client."""

    aws_region: str = Field(default=DEFAULT_AWS_REGION, description="AWS region for Athena service")
    aws_profile: str = Field(default="legacy", description="AWS profile to use for authentication")
    database: str = Field(default=DEFAULT_DATABASE, description="Default database for queries")
    s3_staging_bucket: str = Field(
        default=DEFAULT_S3_BUCKET, description="S3 bucket for query results staging"
    )
    s3_staging_path: str = Field(
        default=DEFAULT_STAGING_DIR, description="S3 path prefix for staging results"
    )
    max_execution_checks: Annotated[int, Field(ge=1, le=3600)] = DEFAULT_MAX_EXECUTION_CHECKS
    sleep_time: Annotated[int, Field(ge=1, le=60)] = DEFAULT_SLEEP_TIME
    query_timeout: Annotated[int, Field(ge=60, le=7200)] = DEFAULT_QUERY_TIMEOUT
    auto_cleanup: bool = Field(
        default=True, description="Automatically cleanup staging files after query"
    )
    enable_result_reuse: bool = Field(default=True, description="Enable Athena result reuse cache")
    result_reuse_max_age_minutes: Annotated[int, Field(ge=1, le=1440)] = 1000

    enable_local_cache: bool = Field(
        default=False, description="Enable local on-disk cache for DataFrame results"
    )
    local_cache_ttl_minutes: Annotated[int, Field(ge=1, le=10080)] = 60
    local_cache_dir: str = Field(
        default=str(Path.home() / ".cache" / "amira" / "athena"),
        description="Directory for local cache files",
    )


class LocalResultCache:
    """Filesystem-backed cache for Athena query DataFrames."""

    def __init__(self, *, cache_dir: str, ttl_minutes: int) -> None:
        self._cache_dir: Path = Path(cache_dir)
        self._ttl: timedelta = timedelta(minutes=ttl_minutes)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, *, query: str, database: str, region: str) -> str:
        payload: dict[str, str] = {
            "query": query,
            "database": database,
            "region": region,
        }
        raw: str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _paths(self, *, key: str) -> tuple[Path, Path]:
        df_path: Path = self._cache_dir / f"{key}.parquet"
        meta_path: Path = self._cache_dir / f"{key}.json"
        return df_path, meta_path

    def load(self, *, query: str, database: str, region: str) -> pl.DataFrame | None:
        key: str = self._key(query=query, database=database, region=region)
        df_path, meta_path = self._paths(key=key)
        if not df_path.exists() or not meta_path.exists():
            return None
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                meta: dict[str, str] = json.load(f)
            ts_str: str = meta.get("timestamp", "")
            ts: datetime = datetime.fromisoformat(ts_str) if ts_str else datetime.min
            if datetime.now(UTC) - ts > self._ttl:
                return None
            return pl.read_parquet(df_path)
        except Exception:
            return None

    def save(self, *, query: str, database: str, region: str, df: pl.DataFrame) -> None:
        key: str = self._key(query=query, database=database, region=region)
        df_path, meta_path = self._paths(key=key)
        df.write_parquet(df_path)
        meta: dict[str, str] = {"timestamp": datetime.now(UTC).isoformat()}
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f)


class LogMessages(StrEnum):
    """Log messages used throughout the client."""

    STARTING_QUERY = "Starting Athena query execution - query_preview={}, database={}"
    QUERY_COMPLETED = "Athena query completed successfully - execution_id={}, rows_returned={}, execution_time_ms={}"
    QUERY_FAILED = "Athena query execution failed - query_preview={}, error={}"
    QUERY_SUBMITTED = "Query submitted to Athena - execution_id={}, s3_output_location={}"
    STATE_CHANGED = "Athena query state changed, execution_id={execution_id}, old_state={old_state}, new_state={new_state}"
    STATUS_CHECK_FAILED = "Failed to check query status - execution_id={}, error={}"
    CLEANUP_SUCCESS = "Cleaned up {file_count} S3 staging files from {prefix}"
    CLEANUP_LIST_FAILED = "Could not list staging files for cleanup from {prefix}: {error}"
    CLEANUP_DELETE_FAILED = "Failed to delete S3 staging files from {prefix}: {error}"
    CLEANUP_ERROR = "Failed to clean up S3 staging files for {filename}: {error}"
    QUERY_CANCELLED = "Query cancellation requested - execution_id={execution_id}"
    RESOURCES_CLOSED = "Athena client resources closed"


class ProgressMessages(StrEnum):
    """Progress messages for UI feedback."""

    EXECUTING_QUERY = "Executing Athena query..."
    DOWNLOADING_RESULTS = "Downloading S3 results..."
    PARSING_RESULTS = "Parsing results..."
    QUERY_RUNNING = "Query running... ({}s)"


class ProductionAthenaClient:
    """Production-ready Athena client with async support and comprehensive error handling."""

    def __init__(self, *, config: AthenaClientConfig) -> None:
        """Initialize the Athena client.

        Args:
            config: Client configuration settings.
        """
        self._config: AthenaClientConfig = config
        # Use module-level logger instead of instance variable
        self._session: aioboto3.Session | None = None
        self._athena_client: Any | None = None

        s3_config = HighPerformanceS3Config(
            aws_profile=config.aws_profile,
            aws_region=config.aws_region,
        )
        self._s3_client: ProductionS3Client = ProductionS3Client(config=s3_config)

    async def _ensure_session(self) -> None:
        """Ensure aioboto3 session is initialized."""
        if self._session is None:
            self._session = aioboto3.Session(
                profile_name=self._config.aws_profile,
                region_name=self._config.aws_region,
            )

    async def _get_athena_client(self) -> Any:
        """Get async Athena client - create fresh client each time."""
        await self._ensure_session()
        assert self._session is not None
        return self._session.client("athena", region_name=self._config.aws_region)

    async def _get_s3_client(self) -> ProductionS3Client:
        """Get the initialized S3 client."""
        return self._s3_client

    async def close(self) -> None:
        """Close async clients and cleanup resources."""
        if self._s3_client:
            await self._s3_client.close()
        self._session = None
        logger.debug(LogMessages.RESOURCES_CLOSED)

    async def execute_query(
        self,
        *,
        query: str,
        database: str | None = None,
        return_dataframe: bool = True,
        progress: Any | None = None,
        task_id: Any | None = None,
    ) -> pl.DataFrame | QueryResult:
        """Execute an Athena query and return results.

        Args:
            query: SQL query to execute.
            database: Database name (uses config default if None).
            return_dataframe: If True, return DataFrame; if False, return QueryResult.

        Returns:
            DataFrame with query results or QueryResult object.

        Raises:
            AthenaClientError: If query execution fails.
            AthenaQueryError: If query has syntax errors.
            AthenaTimeoutError: If query times out.
        """
        database = database or self._config.database

        logger.info(
            LogMessages.STARTING_QUERY.format(
                query[:NUM_QUERIES_TO_PREVIEW] + "..."
                if len(query) > NUM_QUERIES_TO_PREVIEW
                else query,
                database,
            )
        )

        try:
            if return_dataframe and self._config.enable_local_cache:
                cache = LocalResultCache(
                    cache_dir=self._config.local_cache_dir,
                    ttl_minutes=self._config.local_cache_ttl_minutes,
                )
                cached_df: pl.DataFrame | None = cache.load(
                    query=query, database=database, region=self._config.aws_region
                )
                if cached_df is not None:
                    logger.info("Returning DataFrame from local cache")
                    return cached_df
            if progress and task_id is not None:
                progress.update(task_id, description=ProgressMessages.EXECUTING_QUERY)

            result: QueryResult = await self._execute_query_with_retry(
                query=query,
                database=database,
                progress=progress,
                task_id=task_id,
            )

            if result.state != QueryState.SUCCEEDED:
                raise AthenaQueryError(
                    f"Query failed with state {result.state}: {result.error_message}"
                )

            if not return_dataframe:
                return result

            if progress and task_id is not None:
                progress.update(task_id, description=ProgressMessages.DOWNLOADING_RESULTS)

            if result.filename:
                dataframe: pl.DataFrame = await self._load_results_from_s3(
                    filename=result.filename, progress=progress, task_id=task_id
                )

                if self._config.auto_cleanup:
                    await self._cleanup_staging_files(filename=result.filename)

                if progress and task_id is not None:
                    progress.update(task_id, description=ProgressMessages.PARSING_RESULTS)

                logger.info(
                    LogMessages.QUERY_COMPLETED.format(
                        result.execution_id,
                        len(dataframe),
                        result.execution_time_ms,
                    )
                )

                if self._config.enable_local_cache:
                    cache = LocalResultCache(
                        cache_dir=self._config.local_cache_dir,
                        ttl_minutes=self._config.local_cache_ttl_minutes,
                    )
                    cache.save(
                        query=query,
                        database=database,
                        region=self._config.aws_region,
                        df=dataframe,
                    )
                return dataframe
            else:
                raise AthenaClientError("Query succeeded but no output file found")

        except Exception as e:
            logger.error(
                LogMessages.QUERY_FAILED.format(
                    query[:NUM_QUERIES_TO_PREVIEW] + "..."
                    if len(query) > NUM_QUERIES_TO_PREVIEW
                    else query,
                    str(e),
                )
            )
            raise
        # Unreachable safeguard for static analysis
        return pl.DataFrame()

    async def _execute_query_with_retry(
        self,
        *,
        query: str,
        database: str,
        progress: Any | None = None,
        task_id: Any | None = None,
    ) -> QueryResult:
        """Execute query with status polling and retry logic."""
        execution_id: str = await self._submit_query(
            query=query,
            database=database,
        )

        return await self._poll_query_status(
            execution_id=execution_id, progress=progress, task_id=task_id
        )

    async def _submit_query(
        self,
        *,
        query: str,
        database: str,
    ) -> str:
        """Submit query for execution."""
        try:
            s3_output_location: str = (
                f"s3://{self._config.s3_staging_bucket}/{self._config.s3_staging_path}/"
            )

            logger.info(f"Query: {query}")
            logger.info(f"Database: {database}")
            logger.info(f"S3 Output Location: {s3_output_location}")

            athena_client: Any = await self._get_athena_client()
            result_reuse_configuration: dict[str, Any] | None = None
            if self._config.enable_result_reuse:
                result_reuse_configuration = {
                    "ResultReuseByAgeConfiguration": {
                        "Enabled": True,
                        "MaxAgeInMinutes": self._config.result_reuse_max_age_minutes,
                    }
                }
            async with athena_client as client:
                response: dict[str, Any] = await client.start_query_execution(
                    QueryString=query,
                    QueryExecutionContext={AthenaResponseKeys.DATABASE: database},
                    ResultConfiguration={AthenaResponseKeys.OUTPUT_LOCATION: s3_output_location},
                    **(
                        {"ResultReuseConfiguration": result_reuse_configuration}
                        if result_reuse_configuration
                        else {}
                    ),
                )

            execution_id: str = response[AthenaResponseKeys.QUERY_EXECUTION_ID]

            logger.debug(LogMessages.QUERY_SUBMITTED.format(execution_id, s3_output_location))

            return execution_id

        except (BotoCoreError, ClientError) as e:
            raise AthenaClientError(f"Failed to submit query: {e}") from e

    async def _poll_query_status(
        self,
        *,
        execution_id: str,
        progress: Any | None = None,
        task_id: Any | None = None,
    ) -> QueryResult:
        """Poll query status until completion or timeout."""
        state: QueryState = QueryState.QUEUED
        checks_remaining: int = self._config.max_execution_checks
        start_time: float = time.time()

        while checks_remaining > 0 and state in (QueryState.RUNNING, QueryState.QUEUED):
            checks_remaining -= 1

            elapsed_time: float = time.time() - start_time
            if elapsed_time > self._config.query_timeout:
                raise AthenaTimeoutError(f"Query timed out after {elapsed_time:.1f} seconds")

            if progress and task_id is not None:
                description = ProgressMessages.QUERY_RUNNING.format(int(elapsed_time))
                progress.update(task_id, description=description)

            try:
                athena_client: Any = await self._get_athena_client()
                async with athena_client as client:
                    response: dict[str, Any] = await client.get_query_execution(
                        QueryExecutionId=execution_id
                    )

                query_execution: dict[str, Any] = response.get(
                    AthenaResponseKeys.QUERY_EXECUTION, {}
                )
                status: dict[str, Any] = query_execution.get(AthenaResponseKeys.STATUS, {})
                new_state: QueryState = QueryState(status.get(AthenaResponseKeys.STATE, "UNKNOWN"))

                if state != new_state:
                    logger.info(
                        LogMessages.STATE_CHANGED.format(
                            execution_id=execution_id,
                            old_state=state,
                            new_state=new_state,
                        )
                    )
                    state = new_state

                if state == QueryState.FAILED:
                    error_message: str = status.get(
                        AthenaResponseKeys.STATE_CHANGE_REASON, "Unknown error"
                    )
                    return QueryResult(
                        execution_id=execution_id,
                        state=state,
                        error_message=error_message,
                    )
                elif state == QueryState.SUCCEEDED:
                    s3_output_location: str | None = query_execution.get(
                        AthenaResponseKeys.RESULT_CONFIGURATION, {}
                    ).get(AthenaResponseKeys.OUTPUT_LOCATION)

                    filename: str | None = None
                    if s3_output_location:
                        filename_match: list[str] = re.findall(r".*/(.*)", s3_output_location)
                        if filename_match:
                            filename = filename_match[0]

                    statistics: dict[str, Any] = query_execution.get(
                        AthenaResponseKeys.STATISTICS, {}
                    )
                    execution_time_ms: float | None = statistics.get(
                        AthenaResponseKeys.ENGINE_EXECUTION_TIME
                    )
                    data_scanned_bytes: int | None = statistics.get(
                        AthenaResponseKeys.DATA_SCANNED_BYTES
                    )

                    return QueryResult(
                        execution_id=execution_id,
                        state=state,
                        s3_output_location=s3_output_location,
                        filename=filename,
                        execution_time_ms=execution_time_ms,
                        data_scanned_bytes=data_scanned_bytes,
                    )

                await asyncio.sleep(self._config.sleep_time)

            except (BotoCoreError, ClientError) as e:
                logger.error(
                    LogMessages.STATUS_CHECK_FAILED.format(
                        execution_id,
                        str(e),
                    )
                )
                raise AthenaClientError(f"Failed to check query status: {e}") from e

        raise AthenaTimeoutError(
            f"Query status polling timed out after {self._config.max_execution_checks} checks"
        )

    async def _load_results_from_s3(
        self,
        *,
        filename: str,
        progress: Any | None = None,
        task_id: Any | None = None,
    ) -> pl.DataFrame:
        """Load Athena query results from S3 staging location into a DataFrame."""
        s3_client: ProductionS3Client = await self._get_s3_client()
        bucket, key = self._parse_s3_url(bucket=self._config.s3_staging_bucket, filename=filename)

        local_path: Path = Path("/tmp") / Path(key).name

        try:
            results: list[S3OperationResult] = await s3_client.download_files_batch(
                [S3DownloadRequest(bucket=bucket, key=key, local_path=str(local_path))],
                progress=progress,
                task_id=task_id,
            )
            if not results or not results[0].success:
                error_msg: str = (
                    results[0].error or "Unknown S3 download error"
                    if results
                    else "Unknown S3 download error"
                )
                raise AthenaClientError(f"Failed to load results from S3: {error_msg}")

            return pl.read_csv(local_path)
        finally:
            if local_path.exists():
                local_path.unlink()

    async def _cleanup_staging_files(self, *, filename: str) -> None:
        """Clean up staging files from S3 after processing."""
        try:
            bucket, prefix = self._parse_s3_url(
                bucket=self._config.s3_staging_bucket, filename=filename
            )
            s3_client: ProductionS3Client = await self._get_s3_client()

            list_results: list[S3OperationResult] = await s3_client.list_objects_batch(
                [S3ListRequest(bucket=bucket, prefix=prefix)]
            )
            if not list_results or not list_results[0].success:
                error_msg: str = list_results[0].error or "Unknown" if list_results else "Unknown"
                logger.warning(
                    LogMessages.CLEANUP_LIST_FAILED.format(
                        prefix=prefix,
                        error=error_msg,
                    )
                )
                return

            objects_to_delete: list[dict[str, Any]] = list_results[0].data.get("objects", [])
            if not objects_to_delete:
                return

            keys_to_delete: list[dict[str, Any]] = [
                {"Key": obj["Key"]} for obj in objects_to_delete
            ]

            delete_result: list[S3OperationResult] = await s3_client.delete_objects_batch(
                bucket, keys_to_delete
            )

            if delete_result and delete_result[0].success:
                logger.info(
                    LogMessages.CLEANUP_SUCCESS.format(
                        file_count=len(keys_to_delete),
                        prefix=prefix,
                    )
                )
            else:
                delete_error: str = (
                    delete_result[0].error or "Unknown" if delete_result else "Unknown"
                )
                logger.warning(
                    LogMessages.CLEANUP_DELETE_FAILED.format(
                        prefix=prefix,
                        error=delete_error,
                    )
                )

        except (BotoCoreError, ClientError) as e:
            logger.warning(
                LogMessages.CLEANUP_ERROR.format(
                    filename=filename,
                    error=str(e),
                )
            )

    async def get_query_status(self, *, execution_id: str) -> QueryExecution:
        """Get the status of a query execution.

        Args:
            execution_id: Query execution ID.

        Returns:
            QueryExecution with current status.

        Raises:
            AthenaClientError: If status check fails.
        """
        try:
            athena_client: Any = await self._get_athena_client()
            async with athena_client as client:
                response: dict[str, Any] = await client.get_query_execution(
                    QueryExecutionId=execution_id
                )

            query_execution: dict[str, Any] = response.get(AthenaResponseKeys.QUERY_EXECUTION, {})
            status: dict[str, Any] = query_execution.get(AthenaResponseKeys.STATUS, {})

            return QueryExecution(
                query=query_execution.get(AthenaResponseKeys.QUERY, ""),
                database=query_execution.get(AthenaResponseKeys.QUERY_EXECUTION_CONTEXT, {}).get(
                    AthenaResponseKeys.DATABASE, ""
                ),
                execution_id=execution_id,
                state=QueryState(status.get("State", "UNKNOWN")),
                s3_output_location=query_execution.get(
                    AthenaResponseKeys.RESULT_CONFIGURATION, {}
                ).get(AthenaResponseKeys.OUTPUT_LOCATION),
                error_message=status.get("StateChangeReason"),
                statistics=query_execution.get("Statistics"),
            )

        except (BotoCoreError, ClientError) as e:
            raise AthenaClientError(f"Failed to get query status: {e}") from e

    async def cancel_query(self, *, execution_id: str) -> bool:
        """Cancel a running query.

        Args:
            execution_id: Query execution ID to cancel.

        Returns:
            True if cancellation was successful.

        Raises:
            AthenaClientError: If cancellation fails.
        """
        try:
            athena_client: Any = await self._get_athena_client()
            async with athena_client as client:
                await client.stop_query_execution(QueryExecutionId=execution_id)

            logger.info(
                LogMessages.QUERY_CANCELLED.format(
                    execution_id=execution_id,
                )
            )

            return True

        except (BotoCoreError, ClientError) as e:
            raise AthenaClientError(f"Failed to cancel query: {e}") from e

    def _parse_s3_url(self, *, bucket: str, filename: str) -> tuple[str, str]:
        """Parse S3 URL to get bucket and key.

        Args:
            bucket: S3 bucket.
            filename: S3 filename.

        Returns:
            tuple: Tuple containing bucket and key.
        """
        if filename.startswith("s3://"):
            parsed = urlparse(filename)
            return parsed.netloc, parsed.path.lstrip("/")
        return bucket, f"{self._config.s3_staging_path}/{filename}"


async def query_athena(
    *,
    query: str,
    database: str = DEFAULT_DATABASE,
    region: str = DEFAULT_AWS_REGION,
    s3_staging_bucket: str = DEFAULT_S3_BUCKET,
    s3_staging_path: str = DEFAULT_STAGING_DIR,
) -> pl.DataFrame:
    """Legacy function for Athena queries.

    Args:
        query: SQL query to execute.
        database: Database name.
        region: AWS region.
        s3_staging_bucket: S3 bucket for staging.
        s3_staging_path: S3 path for staging.

    Returns:
        DataFrame with query results.
    """
    enable_reuse_env: str | None = os.environ.get("ATHENA_CACHE_ENABLED", "true")
    max_age_env: str | None = os.environ.get("ATHENA_CACHE_MAX_AGE_MINUTES", "1000")
    enable_reuse: bool = (enable_reuse_env or "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        max_age_minutes: int = max(1, min(1440, int(max_age_env or "60")))
    except Exception:
        max_age_minutes = 60

    local_cache_enabled_env: str | None = os.environ.get("ATHENA_LOCAL_CACHE_ENABLED", "true")
    local_cache_ttl_env: str | None = os.environ.get("ATHENA_LOCAL_CACHE_TTL_MINUTES", "60")
    local_cache_dir_env: str | None = os.environ.get("ATHENA_LOCAL_CACHE_DIR")
    local_cache_enabled: bool = (local_cache_enabled_env or "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        local_cache_ttl: int = max(1, min(10080, int(local_cache_ttl_env or "60")))
    except Exception:
        local_cache_ttl = 60

    config: AthenaClientConfig = AthenaClientConfig(
        aws_region=region,
        aws_profile="legacy",
        database=database,
        s3_staging_bucket=s3_staging_bucket,
        s3_staging_path=s3_staging_path,
        enable_result_reuse=enable_reuse,
        result_reuse_max_age_minutes=max_age_minutes,
        enable_local_cache=local_cache_enabled,
        local_cache_ttl_minutes=local_cache_ttl,
        local_cache_dir=local_cache_dir_env or str(Path.home() / ".cache" / "amira" / "athena"),
    )

    client: ProductionAthenaClient = ProductionAthenaClient(config=config)
    try:
        result = await client.execute_query(query=query, database=database, return_dataframe=True)
        assert isinstance(result, pl.DataFrame)
        return result
    finally:
        await client.close()
