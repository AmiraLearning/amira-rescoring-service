"""High-performance S3 client with advanced optimizations.

This module provides a S3 service with connection pooling,
concurrent operations, batch processing, and comprehensive performance
optimizations for system-wide use.
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Awaitable

import aioboto3
from botocore.client import Config as BotoConfig
from pydantic import BaseModel, Field
from rich.progress import Progress

from loguru import logger

DEFAULT_MAX_CONNECTIONS: Final[int] = 1000  # Increased for cloud deployment
DEFAULT_MAX_CONNECTIONS_PER_HOST: Final[int] = 500  # Increased for cloud deployment
DEFAULT_CONNECTION_TIMEOUT: Final[int] = 60
DEFAULT_READ_TIMEOUT: Final[int] = 300
DEFAULT_MAX_CONCURRENT_DOWNLOADS: Final[int] = 500  # Massive concurrency for cloud
DEFAULT_MAX_CONCURRENT_UPLOADS: Final[int] = 200  # Increased upload concurrency
DEFAULT_MAX_CONCURRENT_OPERATIONS: Final[int] = 1000  # Total operations for cloud
DEFAULT_MULTIPART_THRESHOLD: Final[int] = 64 * 1024 * 1024  # 64MB
DEFAULT_MULTIPART_CHUNKSIZE: Final[int] = 16 * 1024 * 1024  # 16MB
DEFAULT_MAX_RETRIES: Final[int] = 5
DEFAULT_RETRY_BACKOFF_BASE: Final[float] = 0.1
DEFAULT_RETRY_BACKOFF_MAX: Final[float] = 60.0


@dataclass
class S3Operation:
    """Represents an S3 operation for batch processing."""

    bucket: str
    key: str
    local_path: str | None = None
    operation_type: str = "download"  # download, upload, head, list
    metadata: dict[str, Any] | None = None


@dataclass
class S3OperationResult:
    """Result of an S3 operation."""

    success: bool
    operation: S3Operation
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0


class HighPerformanceS3Config(BaseModel):
    """Configuration for high-performance S3 client."""

    aws_profile: str = Field(
        default="legacy", description="AWS profile for authentication"
    )
    aws_region: str = Field(default="us-east-1", description="AWS region")

    max_connections: int = Field(
        default=DEFAULT_MAX_CONNECTIONS,
        ge=50,
        le=5000,  # Increased limit for cloud deployment
        description="Maximum total connections in pool (optimized for cloud)",
    )
    max_connections_per_host: int = Field(
        default=DEFAULT_MAX_CONNECTIONS_PER_HOST,
        ge=20,
        le=2000,  # Increased limit for cloud deployment
        description="Maximum connections per host (optimized for cloud)",
    )
    connection_timeout: int = Field(
        default=DEFAULT_CONNECTION_TIMEOUT,
        ge=10,
        le=300,
        description="Connection timeout in seconds",
    )
    read_timeout: int = Field(
        default=DEFAULT_READ_TIMEOUT,
        ge=30,
        le=1800,
        description="Read timeout in seconds",
    )

    max_concurrent_downloads: int = Field(
        default=DEFAULT_MAX_CONCURRENT_DOWNLOADS,
        ge=10,
        le=2000,  # Increased for cloud deployment
        description="Maximum concurrent downloads (optimized for cloud)",
    )
    max_concurrent_uploads: int = Field(
        default=DEFAULT_MAX_CONCURRENT_UPLOADS,
        ge=5,
        le=1000,  # Increased for cloud deployment
        description="Maximum concurrent uploads (optimized for cloud)",
    )
    max_concurrent_operations: int = Field(
        default=DEFAULT_MAX_CONCURRENT_OPERATIONS,
        ge=20,
        le=5000,  # Increased for cloud deployment
        description="Maximum concurrent operations (optimized for cloud)",
    )

    multipart_threshold: int = Field(
        default=DEFAULT_MULTIPART_THRESHOLD,
        ge=5 * 1024 * 1024,
        le=1024 * 1024 * 1024,
        description="Multipart upload threshold in bytes",
    )
    multipart_chunksize: int = Field(
        default=DEFAULT_MULTIPART_CHUNKSIZE,
        ge=5 * 1024 * 1024,
        le=100 * 1024 * 1024,
        description="Multipart chunk size in bytes",
    )
    max_bandwidth: int | None = Field(
        default=None, description="Maximum bandwidth in bytes/sec (None for unlimited)"
    )

    max_retries: int = Field(
        default=DEFAULT_MAX_RETRIES, ge=1, le=10, description="Maximum retry attempts"
    )
    retry_backoff_base: float = Field(
        default=DEFAULT_RETRY_BACKOFF_BASE,
        ge=0.01,
        le=1.0,
        description="Base retry backoff time",
    )
    retry_backoff_max: float = Field(
        default=DEFAULT_RETRY_BACKOFF_MAX,
        ge=1.0,
        le=300.0,
        description="Maximum retry backoff time",
    )

    buffer_size: int = Field(
        default=8192,
        ge=1024,
        le=65536,
        description="Buffer size for streaming operations",
    )
    client_pool_size: int = Field(
        default=20, ge=5, le=100, description="Size of S3 client pool"
    )


class ProductionS3Client:
    """S3 client with optimizations."""

    def __init__(self, *, config: HighPerformanceS3Config) -> None:
        """Initialize high-performance S3 client.

        Args:
            config: S3 client configuration.
        """
        self._config: HighPerformanceS3Config = config
        self._logger = logger

        self._session: aioboto3.Session | None = None
        self._client_pool: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=config.client_pool_size
        )
        self._clients_created: int = 0

        self._download_semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        self._upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)
        self._operation_semaphore = asyncio.Semaphore(config.max_concurrent_operations)

        self._operation_count: int = 0
        self._total_bytes_transferred: int = 0
        self._total_operation_time: float = 0.0

    async def _ensure_session(self) -> None:
        """Ensure aioboto3 session is initialized with optimized settings."""
        if self._session is None:
            self._session = aioboto3.Session(
                profile_name=self._config.aws_profile,
                region_name=self._config.aws_region,
            )

            self._logger.info(
                "High-performance S3 session initialized",
                component="s3_client",
                max_concurrent_downloads=self._config.max_concurrent_downloads,
                max_concurrent_uploads=self._config.max_concurrent_uploads,
                client_pool_size=self._config.client_pool_size,
            )

    async def _get_client(self) -> Any:
        """Get S3 client from pool or create new one."""
        await self._ensure_session()
        assert self._session is not None

        try:
            client = self._client_pool.get_nowait()
            return client
        except asyncio.QueueEmpty:
            if self._clients_created < self._config.client_pool_size:
                client_context = self._session.client(
                    "s3",
                    config=BotoConfig(
                        max_pool_connections=self._config.max_connections_per_host,
                        retries={"max_attempts": 0},
                        read_timeout=self._config.read_timeout,
                        connect_timeout=self._config.connection_timeout,
                    ),
                )
                client = await client_context.__aenter__()
                self._clients_created += 1
                return client
            else:
                return await self._client_pool.get()

    async def _return_client(self, client: Any) -> None:
        """Return client to pool."""
        try:
            self._client_pool.put_nowait(client)
        except asyncio.QueueFull:
            await client.close()

    async def close(self) -> None:
        """Close all clients and cleanup resources."""
        while not self._client_pool.empty():
            try:
                client = self._client_pool.get_nowait()
                await client.close()
            except asyncio.QueueEmpty:
                break

        self._session = None
        self._clients_created = 0

        self._logger.info(
            "S3 client closed",
            component="s3_client",
            operations_processed=self._operation_count,
            total_bytes_transferred=self._total_bytes_transferred,
            avg_operation_time_ms=self._total_operation_time
            / max(1, self._operation_count)
            * 1000,
        )

    async def download_files_batch(
        self,
        operations: list[tuple[str, str, str]],
        progress: Progress | None = None,
        task_id: Any | None = None,
    ) -> list[S3OperationResult]:
        """Download multiple files concurrently with optimal performance.

        Args:
            operations: List of (bucket, key, local_path) tuples.
            progress: Rich progress bar object.
            task_id: Task ID for the progress bar.

        Returns:
            List of operation results.
        """
        if not operations:
            return []

        self._logger.info(
            "Starting batch download",
            component="s3_client",
            operation_count=len(operations),
        )

        s3_operations = [
            S3Operation(
                bucket=bucket, key=key, local_path=local_path, operation_type="download"
            )
            for bucket, key, local_path in operations
        ]

        tasks: list[Awaitable[S3OperationResult]] = [
            self._download_single_with_semaphore(op, progress=progress, task_id=task_id)
            for op in s3_operations
        ]

        results: list[S3OperationResult | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        processed_results: list[S3OperationResult] = []
        for result in results:
            if isinstance(result, Exception):
                processed_results.append(
                    S3OperationResult(
                        success=False,
                        operation=S3Operation(
                            bucket="", key="", operation_type="download"
                        ),
                        error=str(result),
                    )
                )
            elif isinstance(result, S3OperationResult):
                processed_results.append(result)

        successful_count = sum(1 for r in processed_results if r.success)
        self._logger.info(
            "Batch download completed",
            component="s3_client",
            total_operations=len(operations),
            successful=successful_count,
            failed=len(operations) - successful_count,
        )

        return processed_results

    async def _download_single_with_semaphore(
        self,
        operation: S3Operation,
        progress: Progress | None = None,
        task_id: Any | None = None,
    ) -> S3OperationResult:
        """Download single file with semaphore control."""
        async with self._download_semaphore:
            return await self._download_single_with_retry(
                operation, progress=progress, task_id=task_id
            )

    async def _download_single_with_retry(
        self,
        operation: S3Operation,
        progress: Progress | None = None,
        task_id: Any | None = None,
    ) -> S3OperationResult:
        """Download single file with retry logic."""
        start_time = time.time()

        for attempt in range(self._config.max_retries):
            try:
                client = await self._get_client()

                try:
                    if operation.local_path is None:
                        raise ValueError(
                            "local_path is required for download operations"
                        )
                    local_path = Path(operation.local_path)
                    local_path.parent.mkdir(parents=True, exist_ok=True)

                    if progress and task_id is not None:
                        try:
                            head_response = await client.head_object(
                                Bucket=operation.bucket, Key=operation.key
                            )
                            total_size = head_response["ContentLength"]
                            progress.update(task_id, total=total_size)
                        except Exception:
                            total_size = None  # Unable to get size

                        class ProgressCallback:
                            def __init__(
                                self,
                                progress_bar: Progress,
                                task: Any,
                                total: int | None,
                            ) -> None:
                                self._progress = progress_bar
                                self._task = task
                                self._total = total
                                self._seen_so_far = 0

                            def __call__(self, bytes_amount: int) -> None:
                                self._seen_so_far += bytes_amount
                                self._progress.update(
                                    self._task, completed=self._seen_so_far
                                )

                        await client.download_file(
                            operation.bucket,
                            operation.key,
                            str(local_path),
                            Callback=ProgressCallback(progress, task_id, total_size),
                        )
                    else:
                        await client.download_file(
                            operation.bucket, operation.key, str(local_path)
                        )

                    duration = time.time() - start_time
                    file_size = local_path.stat().st_size if local_path.exists() else 0

                    self._operation_count += 1
                    self._total_bytes_transferred += file_size
                    self._total_operation_time += duration

                    return S3OperationResult(
                        success=True,
                        operation=operation,
                        data={"file_size": file_size},
                        duration_ms=duration * 1000,
                    )

                finally:
                    await self._return_client(client)

            except Exception as e:
                self._logger.warning(
                    "Download attempt failed",
                    component="s3_client",
                    bucket=operation.bucket,
                    key=operation.key,
                    attempt=attempt + 1,
                    max_retries=self._config.max_retries,
                    error=str(e),
                    error_type=type(e).__name__,
                )

                if attempt == self._config.max_retries - 1:
                    duration = time.time() - start_time
                    return S3OperationResult(
                        success=False,
                        operation=operation,
                        error=str(e),
                        duration_ms=duration * 1000,
                    )

                backoff_time = min(
                    self._config.retry_backoff_base * (2**attempt),
                    self._config.retry_backoff_max,
                )
                await asyncio.sleep(backoff_time)

        return S3OperationResult(
            success=False, operation=operation, error="Max retries exceeded"
        )

    async def upload_files_batch(
        self, operations: list[tuple[str, str, str]]
    ) -> list[S3OperationResult]:
        """Upload multiple files concurrently with optimal performance.

        Args:
            operations: List of (local_path, bucket, key) tuples.

        Returns:
            List of operation results.
        """
        if not operations:
            return []

        self._logger.info(
            "Starting batch upload",
            component="s3_client",
            operation_count=len(operations),
        )

        s3_operations: list[S3Operation] = [
            S3Operation(
                bucket=bucket, key=key, local_path=local_path, operation_type="upload"
            )
            for local_path, bucket, key in operations
        ]

        tasks: list[Awaitable[S3OperationResult]] = [
            self._upload_single_with_semaphore(op) for op in s3_operations
        ]

        results: list[S3OperationResult | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        processed_results: list[S3OperationResult] = []
        for result in results:
            if isinstance(result, Exception):
                processed_results.append(
                    S3OperationResult(
                        success=False,
                        operation=S3Operation(
                            bucket="", key="", operation_type="upload"
                        ),
                        error=str(result),
                    )
                )
            elif isinstance(result, S3OperationResult):
                processed_results.append(result)

        successful_count = sum(1 for r in processed_results if r.success)
        self._logger.info(
            "Batch upload completed",
            component="s3_client",
            total_operations=len(operations),
            successful=successful_count,
            failed=len(operations) - successful_count,
        )

        return processed_results

    async def _upload_single_with_semaphore(
        self, operation: S3Operation
    ) -> S3OperationResult:
        """Upload single file with semaphore control."""
        async with self._upload_semaphore:
            return await self._upload_single_with_retry(operation)

    async def _upload_single_with_retry(
        self, operation: S3Operation
    ) -> S3OperationResult:
        """Upload single file with retry logic."""
        start_time = time.time()

        for attempt in range(self._config.max_retries):
            try:
                client = await self._get_client()

                try:
                    if operation.local_path is None:
                        raise ValueError(
                            "local_path is required for download operations"
                        )
                    local_path = Path(operation.local_path)

                    if not local_path.exists():
                        return S3OperationResult(
                            success=False,
                            operation=operation,
                            error=f"Local file does not exist: {local_path}",
                        )

                    await client.upload_file(
                        str(local_path), operation.bucket, operation.key
                    )

                    duration = time.time() - start_time
                    file_size = local_path.stat().st_size

                    self._operation_count += 1
                    self._total_bytes_transferred += file_size
                    self._total_operation_time += duration

                    return S3OperationResult(
                        success=True,
                        operation=operation,
                        data={"file_size": file_size},
                        duration_ms=duration * 1000,
                    )

                finally:
                    await self._return_client(client)

            except Exception as e:
                self._logger.warning(
                    "Upload attempt failed",
                    component="s3_client",
                    bucket=operation.bucket,
                    key=operation.key,
                    attempt=attempt + 1,
                    max_retries=self._config.max_retries,
                    error=str(e),
                )

                if attempt == self._config.max_retries - 1:
                    duration = time.time() - start_time
                    return S3OperationResult(
                        success=False,
                        operation=operation,
                        error=str(e),
                        duration_ms=duration * 1000,
                    )

                backoff_time = min(
                    self._config.retry_backoff_base * (2**attempt),
                    self._config.retry_backoff_max,
                )
                await asyncio.sleep(backoff_time)

        return S3OperationResult(
            success=False, operation=operation, error="Max retries exceeded"
        )

    async def list_objects_batch(
        self, requests: list[tuple[str, str]]
    ) -> list[S3OperationResult]:
        """List objects from multiple buckets/prefixes concurrently.

        Args:
            requests: List of (bucket, prefix) tuples.

        Returns:
            List of operation results with object listings.
        """
        if not requests:
            return []

        operations: list[S3Operation] = [
            S3Operation(bucket=bucket, key=prefix, operation_type="list")
            for bucket, prefix in requests
        ]

        tasks: list[Awaitable[S3OperationResult]] = [
            self._list_objects_single(op) for op in operations
        ]

        results: list[S3OperationResult | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        processed_results: list[S3OperationResult] = []
        for result in results:
            if isinstance(result, Exception):
                processed_results.append(
                    S3OperationResult(
                        success=False,
                        operation=S3Operation(bucket="", key="", operation_type="list"),
                        error=str(result),
                    )
                )
            elif isinstance(result, S3OperationResult):
                processed_results.append(result)

        return processed_results

    async def _list_objects_single(self, operation: S3Operation) -> S3OperationResult:
        """List objects for single bucket/prefix."""
        async with self._operation_semaphore:
            start_time = time.time()

            try:
                client = await self._get_client()

                try:
                    response = await client.list_objects_v2(
                        Bucket=operation.bucket, Prefix=operation.key
                    )

                    objects: list[Any] = response.get("Contents", [])
                    duration = time.time() - start_time

                    return S3OperationResult(
                        success=True,
                        operation=operation,
                        data={"objects": objects, "count": len(objects)},
                        duration_ms=duration * 1000,
                    )

                finally:
                    await self._return_client(client)

            except Exception as e:
                duration = time.time() - start_time
                return S3OperationResult(
                    success=False,
                    operation=operation,
                    error=str(e),
                    duration_ms=duration * 1000,
                )

    async def head_objects_batch(
        self, requests: list[tuple[str, str]]
    ) -> list[S3OperationResult]:
        """Get metadata for multiple objects concurrently.

        Args:
            requests: List of (bucket, key) tuples.

        Returns:
            List of operation results with object metadata.
        """
        if not requests:
            return []

        operations: list[S3Operation] = [
            S3Operation(bucket=bucket, key=key, operation_type="head")
            for bucket, key in requests
        ]

        tasks: list[Awaitable[S3OperationResult]] = [
            self._head_object_single(op) for op in operations
        ]

        results: list[S3OperationResult | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        processed_results: list[S3OperationResult] = []
        for result in results:
            if isinstance(result, Exception):
                processed_results.append(
                    S3OperationResult(
                        success=False,
                        operation=S3Operation(bucket="", key="", operation_type="head"),
                        error=str(result),
                    )
                )
            elif isinstance(result, S3OperationResult):
                processed_results.append(result)

        return processed_results

    async def _head_object_single(self, operation: S3Operation) -> S3OperationResult:
        """Get metadata for single object."""
        async with self._operation_semaphore:
            start_time: float = time.time()

            try:
                client = await self._get_client()

                try:
                    response = await client.head_object(
                        Bucket=operation.bucket, Key=operation.key
                    )

                    duration_success: float = time.time() - start_time

                    return S3OperationResult(
                        success=True,
                        operation=operation,
                        data={"metadata": response},
                        duration_ms=duration_success * 1000,
                    )

                finally:
                    await self._return_client(client)

            except Exception as e:
                duration_error: float = time.time() - start_time
                return S3OperationResult(
                    success=False,
                    operation=operation,
                    error=str(e),
                    duration_ms=duration_error * 1000,
                )

    async def delete_objects_batch(
        self, bucket: str, keys: list[dict[str, str]]
    ) -> list[S3OperationResult]:
        """Delete multiple objects concurrently."""
        if not keys:
            return []

        self._logger.info(
            "Starting batch delete",
            bucket=bucket,
            object_count=len(keys),
        )

        operations = [
            S3Operation(bucket=bucket, key=key_dict["Key"], operation_type="delete")
            for key_dict in keys
        ]

        tasks = [self._delete_single_with_semaphore(op) for op in operations]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results: list[S3OperationResult] = []
        for result in results:
            if isinstance(result, Exception):
                processed_results.append(
                    S3OperationResult(
                        success=False,
                        operation=S3Operation(
                            bucket=bucket, key="", operation_type="delete"
                        ),
                        error=str(result),
                    )
                )
            elif isinstance(result, S3OperationResult):
                processed_results.append(result)

        successful_count = sum(1 for r in processed_results if r.success)
        self._logger.info(
            "Batch delete completed",
            bucket=bucket,
            total_operations=len(keys),
            successful=successful_count,
            failed=len(keys) - successful_count,
        )

        return processed_results

    async def _delete_single_with_semaphore(
        self, operation: S3Operation
    ) -> S3OperationResult:
        """Delete single object with semaphore control."""
        async with self._operation_semaphore:
            return await self._delete_single_with_retry(operation)

    async def _delete_single_with_retry(
        self, operation: S3Operation
    ) -> S3OperationResult:
        """Delete single object with retry logic."""
        start_time = time.time()
        for attempt in range(self._config.max_retries):
            try:
                client = await self._get_client()
                try:
                    await client.delete_object(
                        Bucket=operation.bucket, Key=operation.key
                    )
                    duration = time.time() - start_time
                    return S3OperationResult(
                        success=True,
                        operation=operation,
                        duration_ms=duration * 1000,
                    )
                finally:
                    await self._return_client(client)
            except Exception as e:
                self._logger.warning(
                    "Delete attempt failed",
                    bucket=operation.bucket,
                    key=operation.key,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == self._config.max_retries - 1:
                    return S3OperationResult(
                        success=False,
                        operation=operation,
                        error=str(e),
                        duration_ms=(time.time() - start_time) * 1000,
                    )
                await asyncio.sleep(
                    min(
                        self._config.retry_backoff_base * (2**attempt),
                        self._config.retry_backoff_max,
                    )
                )
        return S3OperationResult(
            success=False, operation=operation, error="Max retries exceeded"
        )

    def get_performance_metrics(self) -> dict[str, Any]:
        """Get performance metrics for monitoring."""
        avg_operation_time: float = self._total_operation_time / max(
            1, self._operation_count
        )

        return {
            "operation_count": self._operation_count,
            "total_bytes_transferred": self._total_bytes_transferred,
            "avg_operation_time_ms": avg_operation_time * 1000,
            "throughput_mbps": (
                self._total_bytes_transferred
                / max(1, self._total_operation_time)
                / 1024
                / 1024
            ),
            "clients_created": self._clients_created,
            "client_pool_size": self._config.client_pool_size,
        }


class S3ClientError(Exception):
    """Base exception for S3 client errors."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        """Initialize S3 client error.

        Args:
            message: Error description.
            **kwargs: Additional arguments passed to Exception.
        """
        super().__init__(message)


class S3OperationError(S3ClientError):
    """Raised when S3 operations fail."""

    def __init__(self, message: str, operation: str, **kwargs: Any) -> None:
        """Initialize S3 operation error.

        Args:
            message: Error description.
            operation: S3 operation that failed.
            **kwargs: Additional arguments passed to S3ClientError.
        """
        super().__init__(message=f"S3 {operation} failed: {message}", **kwargs)
        self.operation = operation
