from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from amira_pyutils.language import LanguageHandling


class AmiraError(Exception):
    """Root class for all distinguished errors raised by this library.

    Args:
        msg: Error message
        retryable: Whether the operation that caused this error can be retried
    """

    def __init__(self, *, msg: str, retryable: bool = True) -> None:
        super().__init__(msg)
        self._retryable = retryable

    @property
    def retryable(self) -> bool:
        """Whether this error indicates a retryable operation."""
        return self._retryable


class WrappedExceptionError(AmiraError):
    """Base class for errors that wrap other exceptions.

    Args:
        exception: The original exception being wrapped
        retryable: Whether the operation that caused this error can be retried
    """

    def __init__(self, *, exception: Exception, retryable: bool = True) -> None:
        super().__init__(msg=f"{type(self).__name__}: {exception}", retryable=retryable)
        self._details = exception

    @property
    def details(self) -> Exception:
        """The original exception that was wrapped."""
        return self._details


class AppsyncError(WrappedExceptionError):
    """Error from AWS AppSync operations.

    Args:
        exception: The original AppSync exception
    """

    def __init__(self, *, exception: Exception) -> None:
        super().__init__(exception=exception)


class FeatureStoreError(WrappedExceptionError):
    """Error from feature store operations.

    Args:
        exception: The original feature store exception
        retryable: Whether the feature store operation can be retried
    """

    def __init__(self, *, exception: Exception, retryable: bool) -> None:
        super().__init__(exception=exception, retryable=retryable)


class BadAmirabetError(AmiraError):
    """Error for invalid AmiraBet character sequences.

    Args:
        invalid_string: The string containing invalid AmiraBet characters
    """

    def __init__(self, *, invalid_string: str) -> None:
        super().__init__(
            msg=f"'{invalid_string}' contains invalid AmiraBet characters",
            retryable=False,
        )


class ModelHostingError(AmiraError):
    """Error from ML model hosting service.

    Args:
        error_message: Error message from the hosting service
    """

    def __init__(self, *, error_message: str) -> None:
        super().__init__(msg=f"Error returned from ml-serving: {error_message}", retryable=False)


class UnsupportedLanguageConversionError(AmiraError):
    """Error for unsupported language conversion operations.

    Args:
        requested_language: The language that was requested but is not supported
        supported_codes: List of supported language codes
    """

    def __init__(
        self, *, requested_language: "LanguageHandling", supported_codes: Sequence[str]
    ) -> None:
        from amira_pyutils.language import LanguageHandling

        supported_languages = [
            lang.value for lang in LanguageHandling if lang.language_code in supported_codes
        ]
        supported_names: Final[str] = ", ".join(supported_languages)

        message = (
            f"Provided language {requested_language.value} is unsupported for "
            f"alphabet translation (supported are {supported_names})"
        )
        super().__init__(msg=message, retryable=False)


class MissingConfigError(AmiraError):
    """Error for missing required configuration keys.

    Args:
        config_key_name: Name of the missing configuration key
    """

    def __init__(self, *, config_key_name: str) -> None:
        super().__init__(msg=f"Missing required config key: {config_key_name}", retryable=False)


class LambdaError(AmiraError):
    """Error from AWS Lambda function invocation.

    Args:
        message: Error message from Lambda invocation
    """

    def __init__(self, message: str) -> None:
        super().__init__(msg=message, retryable=False)


# Database and Storage Errors
class DatabaseError(AmiraError):
    """Base class for database-related errors."""


class DynamoDBError(DatabaseError):
    """Error from DynamoDB operations.

    Args:
        operation: The DynamoDB operation that failed
        table_name: Name of the table involved
        error_details: Additional error information
        retryable: Whether the operation can be retried
    """

    def __init__(
        self,
        *,
        operation: str,
        table_name: str,
        error_details: str,
        retryable: bool = True,
    ) -> None:
        msg = f"DynamoDB {operation} failed for table '{table_name}': {error_details}"
        super().__init__(msg=msg, retryable=retryable)
        self.operation = operation
        self.table_name = table_name


class S3Error(AmiraError):
    """Error from S3 operations.

    Args:
        operation: The S3 operation that failed
        bucket: S3 bucket name
        key: S3 object key (optional)
        error_details: Additional error information
        retryable: Whether the operation can be retried
    """

    def __init__(
        self,
        *,
        operation: str,
        bucket: str,
        key: str | None = None,
        error_details: str,
        retryable: bool = True,
    ) -> None:
        location = f"s3://{bucket}/{key}" if key else f"s3://{bucket}"
        msg = f"S3 {operation} failed for {location}: {error_details}"
        super().__init__(msg=msg, retryable=retryable)
        self.operation = operation
        self.bucket = bucket
        self.key = key


# Service Communication Errors
class ServiceError(AmiraError):
    """Base class for external service errors."""


class HTTPError(ServiceError):
    """HTTP communication error.

    Args:
        url: The URL that failed
        status_code: HTTP status code
        method: HTTP method used
        response_text: Response body text
        retryable: Whether the request can be retried
    """

    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        method: str = "GET",
        response_text: str = "",
        retryable: bool = True,
    ) -> None:
        msg = f"HTTP {method} {url} failed with status {status_code}"
        if response_text:
            msg += f": {response_text}"
        super().__init__(msg=msg, retryable=retryable)
        self.url = url
        self.status_code = status_code
        self.method = method


class CircuitBreakerError(ServiceError):
    """Error when circuit breaker is open.

    Args:
        service_name: Name of the protected service
        failure_count: Current failure count
        last_failure_time: When the last failure occurred
    """

    def __init__(self, *, service_name: str, failure_count: int, last_failure_time: float) -> None:
        msg = (
            f"Circuit breaker for '{service_name}' is OPEN. "
            f"Failures: {failure_count}, Last failure: {last_failure_time}"
        )
        super().__init__(msg=msg, retryable=False)  # Don't retry when circuit is open
        self.service_name = service_name
        self.failure_count = failure_count


class RateLimitError(ServiceError):
    """Error when rate limit is exceeded.

    Args:
        service_name: Name of the rate-limited service
        retry_after: Seconds to wait before retrying
        current_rate: Current request rate
        limit: The rate limit that was exceeded
    """

    def __init__(
        self,
        *,
        service_name: str,
        retry_after: float,
        current_rate: float,
        limit: float,
    ) -> None:
        msg = (
            f"Rate limit exceeded for '{service_name}'. "
            f"Current rate: {current_rate:.2f}/s, Limit: {limit:.2f}/s. "
            f"Retry after: {retry_after:.1f}s"
        )
        super().__init__(msg=msg, retryable=True)
        self.service_name = service_name
        self.retry_after = retry_after


# Data Processing Errors
class DataProcessingError(AmiraError):
    """Base class for data processing errors."""


class ValidationError(DataProcessingError):
    """Error from data validation.

    Args:
        field_name: Name of the field that failed validation
        field_value: Value that failed validation
        constraint: The validation constraint that was violated
        expected_format: Description of expected format
    """

    def __init__(
        self,
        *,
        field_name: str,
        field_value: str,
        constraint: str,
        expected_format: str | None = None,
    ) -> None:
        msg = f"Validation failed for field '{field_name}' with value '{field_value}': {constraint}"
        if expected_format:
            msg += f". Expected format: {expected_format}"
        super().__init__(msg=msg, retryable=False)
        self.field_name = field_name
        self.field_value = field_value


class SerializationError(DataProcessingError):
    """Error during data serialization/deserialization.

    Args:
        data_type: Type of data being processed
        operation: Whether serializing or deserializing
        error_details: Additional error information
    """

    def __init__(self, *, data_type: str, operation: str, error_details: str) -> None:
        msg = f"Failed to {operation} {data_type}: {error_details}"
        super().__init__(msg=msg, retryable=False)
        self.data_type = data_type
        self.operation = operation


# ASR and Audio Processing Errors
class ASRError(AmiraError):
    """Base class for Automatic Speech Recognition errors."""


class AudioReplayError(ASRError):
    """Error during audio replay operations.

    Args:
        msg: Error message describing the audio replay failure
        audio_path: Path to the audio file that caused the error
        retryable: Whether the audio processing can be retried
    """

    def __init__(self, *, msg: str, audio_path: str | None = None, retryable: bool = True) -> None:
        full_msg = msg
        if audio_path:
            full_msg = f"{msg} (Audio: {audio_path})"
        super().__init__(msg=full_msg, retryable=retryable)
        self.audio_path = audio_path


class TranscriptionError(ASRError):
    """Error during speech transcription.

    Args:
        engine: ASR engine that failed (e.g., 'google', 'kaldi', 'wav2vec')
        error_details: Details about the transcription failure
        audio_duration: Duration of audio in seconds (optional)
        retryable: Whether transcription can be retried
    """

    def __init__(
        self,
        *,
        engine: str,
        error_details: str,
        audio_duration: float | None = None,
        retryable: bool = True,
    ) -> None:
        msg = f"Transcription failed with {engine} engine: {error_details}"
        if audio_duration:
            msg += f" (Audio duration: {audio_duration:.1f}s)"
        super().__init__(msg=msg, retryable=retryable)
        self.engine = engine
        self.audio_duration = audio_duration


# Resource and Infrastructure Errors
class ResourceError(AmiraError):
    """Base class for resource-related errors."""


class ConnectionPoolError(ResourceError):
    """Error from connection pool operations.

    Args:
        pool_name: Name of the connection pool
        operation: Operation that failed (e.g., 'acquire', 'release')
        pool_size: Current size of the pool
        error_details: Additional error information
    """

    def __init__(
        self, *, pool_name: str, operation: str, pool_size: int, error_details: str
    ) -> None:
        msg = (
            f"Connection pool '{pool_name}' {operation} failed. "
            f"Pool size: {pool_size}. Details: {error_details}"
        )
        super().__init__(msg=msg, retryable=True)
        self.pool_name = pool_name
        self.pool_size = pool_size


class TimeoutError(ResourceError):
    """Error when an operation times out.

    Args:
        operation: Description of the operation that timed out
        timeout_seconds: The timeout value that was exceeded
        elapsed_seconds: How long the operation actually took
    """

    def __init__(self, *, operation: str, timeout_seconds: float, elapsed_seconds: float) -> None:
        msg = (
            f"Operation '{operation}' timed out after {elapsed_seconds:.1f}s "
            f"(timeout: {timeout_seconds:.1f}s)"
        )
        super().__init__(msg=msg, retryable=True)
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
