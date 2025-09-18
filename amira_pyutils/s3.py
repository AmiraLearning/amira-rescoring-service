from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from io import StringIO
from pathlib import Path
from typing import Any, Final, ParamSpec, TypeVar, cast
from urllib.parse import urlparse

import aioboto3
import orjson as json
import pandas as pd
from botocore.exceptions import ClientError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

P = ParamSpec("P")
T = TypeVar("T")
import asyncio as _asyncio

import aiohttp as _aiohttp

from amira_pyutils.jsonable import JsonSerializable
from amira_pyutils.logging import get_logger

logger = get_logger(name=__name__)

S3_SCHEME: Final[str] = "s3"
S3_URI_PREFIX: Final[str] = "s3://"
DEFAULT_DELIMITER: Final[str] = "/"
NOT_FOUND_ERROR_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey"})
RETRYABLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "InternalError",
        "ServiceUnavailable",
        "RequestTimeout",
        "SlowDown",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
    }
)


def _typed_retry(*args: Any, **kwargs: Any) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Typed wrapper for tenacity retry decorator."""
    return retry(*args, **kwargs)  # type: ignore[no-any-return]


def _is_retryable_s3_error(exception: BaseException) -> bool:
    """Check if S3 error is retryable.

    Args:
        exception: The exception to check.

    Returns:
        True if the exception is retryable, False otherwise.
    """
    match exception:
        case ClientError():
            error_code = exception.response.get("Error", {}).get("Code", "")
            return error_code in RETRYABLE_ERROR_CODES
        case _aiohttp.ClientError() | _asyncio.TimeoutError():
            return True
        case OSError() | ConnectionError() | TimeoutError():
            return True
        case _:
            return False


class S3ErrorType(StrEnum):
    """S3 error types."""

    NOT_FOUND = "NoSuchKey"
    ACCESS_DENIED = "AccessDenied"
    INVALID_BUCKET_NAME = "InvalidBucketName"


@dataclass(frozen=True)
class S3Address:
    """S3 object address."""

    bucket: str
    key: str

    @classmethod
    def from_uri(cls, *, uri: str) -> "S3Address":
        """Create S3Address from URI.

        Args:
            uri: S3 URI in format s3://bucket/key

        Returns:
            S3Address instance

        Raises:
            ValueError: If URI is not a valid S3 URI
        """
        if not cls.is_s3_uri(uri=uri):
            raise ValueError(f"Invalid S3 URI: {uri}")

        parsed = urlparse(uri, allow_fragments=False)
        return cls(bucket=parsed.netloc, key=parsed.path.lstrip("/"))

    @staticmethod
    def is_s3_uri(*, uri: str) -> bool:
        """Check if URI is a valid S3 URI.

        Args:
            uri: URI to check

        Returns:
            True if URI starts with s3://
        """
        return uri.startswith(S3_URI_PREFIX)


@dataclass(frozen=True)
class S3ObjectMetadata:
    """S3 object metadata."""

    content_type: str | None = None
    content_length: int | None = None
    content_encoding: str | None = None
    etag: str | None = None
    version_id: str | None = None
    server_side_encryption: str | None = None
    storage_class: str | None = None
    metadata: dict[str, str] | None = None
    last_modified: datetime | None = None
    expires: datetime | None = None
    cache_control: str | None = None
    content_disposition: str | None = None
    content_language: str | None = None

    @classmethod
    def from_head_object_response(cls, response: dict[str, Any]) -> "S3ObjectMetadata":
        """Create from S3 HeadObject response."""
        return cls(
            content_type=response.get("ContentType"),
            content_length=response.get("ContentLength"),
            content_encoding=response.get("ContentEncoding"),
            etag=response.get("ETag"),
            version_id=response.get("VersionId"),
            server_side_encryption=response.get("ServerSideEncryption"),
            storage_class=response.get("StorageClass"),
            metadata=response.get("Metadata"),
            last_modified=response.get("LastModified"),
            expires=response.get("Expires"),
            cache_control=response.get("CacheControl"),
            content_disposition=response.get("ContentDisposition"),
            content_language=response.get("ContentLanguage"),
        )


@dataclass(frozen=True)
class S3ObjectInfo:
    """Complete S3 object information."""

    bucket: str
    key: str
    metadata: S3ObjectMetadata
    address: S3Address

    @classmethod
    def create(cls, *, bucket: str, key: str, metadata: S3ObjectMetadata) -> "S3ObjectInfo":
        """Create S3ObjectInfo with computed address."""
        return cls(
            bucket=bucket,
            key=key,
            metadata=metadata,
            address=S3Address(bucket=bucket, key=key),
        )


@dataclass(frozen=True)
class S3Config:
    """S3 client configuration."""

    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    profile_name: str | None = None
    region_name: str | None = None


class S3Service:
    """Async S3 service for file operations."""

    def __init__(self, *, config: S3Config | None = None) -> None:
        """Initialize S3 service.

        Args:
            config: S3 configuration
        """
        self._config = config or S3Config()

    def _get_client(self) -> Any:
        """Get async S3 client context manager."""
        session = aioboto3.Session(
            aws_access_key_id=self._config.aws_access_key_id,
            aws_secret_access_key=self._config.aws_secret_access_key,
            profile_name=self._config.profile_name,
            region_name=self._config.region_name,
        )
        return session.client("s3")

    async def exists(self, *, bucket: str, key: str) -> bool:
        """Check if S3 object exists.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            True if object exists
        """
        try:
            async with self._get_client() as client:
                await client.head_object(Bucket=bucket, Key=key)
                return True
        except ClientError as e:
            if e.response["Error"]["Code"] in NOT_FOUND_ERROR_CODES:
                return False
            raise

    @_typed_retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_s3_error),
        reraise=True,
    )
    async def download_file(self, *, bucket: str, key: str, filename: str | Path) -> None:
        """Download file from S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key
            filename: Local file path

        Raises:
            ClientError: If download fails
        """
        logger.info(f"Downloading {filename} from s3://{bucket}/{key}")
        async with self._get_client() as client:
            with open(filename, "wb") as f:
                await client.download_fileobj(bucket, key, f)

    @_typed_retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_s3_error),
        reraise=True,
    )
    async def upload_file(self, *, bucket: str, key: str, filename: str | Path) -> None:
        """Upload file to S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key
            filename: Local file path

        Raises:
            ClientError: If upload fails
        """
        logger.info(f"Uploading {filename} to s3://{bucket}/{key}")
        async with self._get_client() as client:
            await client.upload_file(str(filename), bucket, key)

    @_typed_retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_s3_error),
        reraise=True,
    )
    async def copy_object(
        self,
        *,
        src_bucket: str,
        src_key: str,
        dest_bucket: str,
        dest_key: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Copy S3 object.

        Args:
            src_bucket: Source bucket name
            src_key: Source object key
            dest_bucket: Destination bucket name
            dest_key: Destination object key
            metadata: Optional metadata to set

        Raises:
            ClientError: If copy fails
        """
        copy_source = {"Bucket": src_bucket, "Key": src_key}

        async with self._get_client() as client:
            extra_args: dict[str, Any] = {}
            if metadata:
                extra_args["Metadata"] = metadata
                extra_args["MetadataDirective"] = "REPLACE"

            await client.copy_object(
                CopySource=copy_source,
                Bucket=dest_bucket,
                Key=dest_key,
                **extra_args,
            )

    @_typed_retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_s3_error),
        reraise=True,
    )
    async def get_json(self, *, bucket: str, key: str) -> JsonSerializable:
        """Get JSON object from S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Deserialized JSON data

        Raises:
            ClientError: If object not found or access denied
        """
        async with self._get_client() as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                content = await stream.read()
                return cast(JsonSerializable, json.loads(content))

    async def maybe_get_json(self, *, bucket: str, key: str) -> JsonSerializable | None:
        """Get JSON object from S3, returning None if not found.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Deserialized JSON data or None if not found
        """
        try:
            return await self.get_json(bucket=bucket, key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in NOT_FOUND_ERROR_CODES:
                return None
            raise

    @_typed_retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_s3_error),
        reraise=True,
    )
    async def save_dataframe(self, *, bucket: str, key: str, df: pd.DataFrame) -> None:
        """Save DataFrame as CSV to S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key
            df: DataFrame to save

        Raises:
            ClientError: If save fails
        """
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False)

        async with self._get_client() as client:
            await client.put_object(Bucket=bucket, Key=key, Body=csv_buffer.getvalue())

    async def save_text(self, *, bucket: str, key: str, content: str) -> None:
        """Save text content to S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key
            content: Text content to save

        Raises:
            ClientError: If save fails
        """
        async with self._get_client() as client:
            await client.put_object(Bucket=bucket, Key=key, Body=content)

    async def load_csv(self, *, bucket: str, key: str) -> pd.DataFrame:
        """Load CSV from S3 as DataFrame.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            DataFrame loaded from CSV

        Raises:
            ClientError: If object not found or access denied
        """
        async with self._get_client() as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                content = await stream.read()
                return pd.read_csv(StringIO(content.decode()))

    async def load_text(self, *, bucket: str, key: str) -> str:
        """Load text content from S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Text content

        Raises:
            ClientError: If object not found or access denied
        """
        async with self._get_client() as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                content = await stream.read()
                return cast(str, content.decode())

    async def maybe_load_text(self, *, bucket: str, key: str) -> str | None:
        """Load text content from S3, returning None if not found.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Text content or None if not found
        """
        try:
            return await self.load_text(bucket=bucket, key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in NOT_FOUND_ERROR_CODES:
                return None
            raise

    async def maybe_load_csv(self, *, bucket: str, key: str) -> pd.DataFrame | None:
        """Load CSV from S3 as DataFrame, returning None if not found.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            DataFrame or None if not found
        """
        try:
            return await self.load_csv(bucket=bucket, key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in NOT_FOUND_ERROR_CODES:
                return None
            raise

    async def delete_object(self, *, bucket: str, key: str) -> None:
        """Delete object from S3.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Raises:
            ClientError: If delete fails
        """
        async with self._get_client() as client:
            await client.delete_object(Bucket=bucket, Key=key)

    @dataclass(frozen=True)
    class S3Object:
        key: str
        last_modified: datetime | None = None

    async def get_object_metadata(self, *, bucket: str, key: str) -> S3ObjectMetadata:
        """Get comprehensive object metadata.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            S3 object metadata

        Raises:
            ClientError: If object doesn't exist or access is denied
        """
        async with self._get_client() as client:
            response = await client.head_object(Bucket=bucket, Key=key)
            return S3ObjectMetadata.from_head_object_response(response)

    async def get_object_info(self, *, bucket: str, key: str) -> S3ObjectInfo:
        """Get complete object information.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Complete S3 object information

        Raises:
            ClientError: If object doesn't exist or access is denied
        """
        metadata = await self.get_object_metadata(bucket=bucket, key=key)
        return S3ObjectInfo.create(bucket=bucket, key=key, metadata=metadata)

    async def list_objects(
        self,
        *,
        bucket: str,
        prefix: str = "",
        delimiter: str = DEFAULT_DELIMITER,
        start_after: str = "",
        include_metadata: bool = False,
    ) -> AsyncGenerator[tuple[str, datetime] | str, None]:
        """List objects in S3 bucket with optional prefix filtering.

        Args:
            bucket: S3 bucket name
            prefix: Object key prefix filter
            delimiter: Virtual directory separator
            start_after: Start listing after this key
            include_metadata: Include last modified timestamp

        Yields:
            Object keys or tuples of (key, last_modified)
        """
        async with self._get_client() as client:
            paginator = client.get_paginator("list_objects_v2")

            prefix = prefix.lstrip(delimiter) if prefix.startswith(delimiter) else prefix
            start_after = (start_after or prefix) if prefix.endswith(delimiter) else start_after

            paginate_kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Prefix": prefix,
                "StartAfter": start_after,
            }
            if delimiter:
                paginate_kwargs["Delimiter"] = delimiter

            async for page in paginator.paginate(**paginate_kwargs):
                for content in page.get("Contents", []):
                    if include_metadata:
                        yield content["Key"], content["LastModified"]
                    else:
                        yield content["Key"]

    async def list_objects_info(
        self,
        *,
        bucket: str,
        prefix: str = "",
        delimiter: str = DEFAULT_DELIMITER,
        start_after: str = "",
    ) -> AsyncGenerator[S3Object, None]:
        """List objects yielding a normalized S3Object.

        Args:
            bucket: S3 bucket name
            prefix: Object key prefix filter
            delimiter: Virtual directory separator
            start_after: Start listing after this key

        Yields:
            S3Object entries with key and optional last_modified
        """
        async for item in self.list_objects(
            bucket=bucket,
            prefix=prefix,
            delimiter=delimiter,
            start_after=start_after,
            include_metadata=True,
        ):
            key, last_modified = cast(tuple[str, datetime], item)
            yield self.S3Object(key=key, last_modified=last_modified)

    async def find_matching_keys(
        self,
        *,
        bucket: str,
        prefix: str = "",
        suffix: str = "",
    ) -> AsyncGenerator[str, None]:
        """Find S3 keys matching prefix and suffix patterns.

        Args:
            bucket: S3 bucket name
            prefix: Key prefix filter
            suffix: Key suffix filter

        Yields:
            Matching object keys
        """
        async with self._get_client() as client:
            paginator = client.get_paginator("list_objects_v2")

            kwargs = {"Bucket": bucket}
            if prefix:
                kwargs["Prefix"] = prefix

            async for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.startswith(prefix) and key.endswith(suffix):
                        yield key
