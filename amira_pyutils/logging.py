from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from loguru import Record
import os
import random
import sys
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import orjson as json
from loguru import logger as _loguru_logger


class LogFormat(StrEnum):
    """Supported logging output formats."""

    STANDARD = "standard"
    JSON = "json"
    EMF = "emf"


class LogLevel(StrEnum):
    """Supported log levels."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class CorrelationInfo:
    """Correlation information for request tracking.

    Args:
        request_id: Unique identifier for the request
        activity_id: Optional activity identifier for context
    """

    request_id: str
    activity_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            object.__setattr__(self, "request_id", uuid.uuid4().hex)

    def __str__(self) -> str:
        if self.activity_id:
            return self.activity_id  # Show only activity ID when available
        return self.request_id


@dataclass(frozen=True)
class LoggerConfig:
    """Configuration for logger setup.

    Args:
        level: Logging level
        format_type: Output format type
        include_correlation: Whether to include correlation info
        cloudwatch_namespace: CloudWatch namespace for EMF metrics
        cloudwatch_dimensions: Default dimensions for EMF metrics
        service: Optional service name to include in log context
        sampling_rate: Log sampling rate (0.0 to 1.0)
        use_stdout: Whether to use stdout instead of stderr
    """

    level: LogLevel = LogLevel.INFO
    format_type: LogFormat = LogFormat.STANDARD
    include_correlation: bool = True
    cloudwatch_namespace: str | None = None
    cloudwatch_dimensions: dict[str, str] | None = None
    service: str | None = None
    sampling_rate: float = 1.0
    use_stdout: bool = False


class CorrelationManager:
    """Thread-safe correlation context manager."""

    def __init__(self) -> None:
        self._correlation = threading.local()

    @property
    def current(self) -> CorrelationInfo | None:
        """Get current correlation info for this thread."""
        return getattr(self._correlation, "info", None)

    def set(self, *, correlation_info: CorrelationInfo) -> None:
        """Set correlation info for current thread.

        Args:
            correlation_info: Correlation information to set
        """
        setattr(self._correlation, "info", correlation_info)

    def clear(self) -> None:
        """Clear correlation info for current thread."""
        setattr(self._correlation, "info", None)

    def create_new(
        self, *, request_id: str | None = None, activity_id: str | None = None
    ) -> CorrelationInfo:
        """Create and set new correlation info.

        Args:
            request_id: Optional specific request ID to use
            activity_id: Optional activity ID for context

        Returns:
            Created correlation info
        """
        correlation_info = CorrelationInfo(
            request_id=request_id or uuid.uuid4().hex, activity_id=activity_id
        )
        self.set(correlation_info=correlation_info)
        return correlation_info


class PropagatingCorrelation:
    """Callable wrapper to propagate correlation between threads/processes.

    Args:
        func: Function to wrap with correlation propagation
        correlation_manager: Manager for correlation context
    """

    def __init__(
        self, *, func: Callable[..., Any], correlation_manager: CorrelationManager
    ) -> None:
        self._func = func
        self._correlation_info = correlation_manager.current
        self._correlation_manager = correlation_manager

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._correlation_info:
            self._correlation_manager.set(correlation_info=self._correlation_info)
        return self._func(*args, **kwargs)


class EMFFormatter:
    """CloudWatch EMF (Embedded Metric Format) formatter.

    Args:
        namespace: CloudWatch namespace for metrics
        default_dimensions: Default dimensions to include
    """

    def __init__(self, *, namespace: str, default_dimensions: dict[str, str] | None = None) -> None:
        self._namespace = namespace
        self._default_dimensions = default_dimensions or {}

    def format_log(self, *, record: dict[str, Any]) -> str:
        """Format log record as EMF JSON.

        Args:
            record: Log record to format

        Returns:
            EMF-formatted JSON string
        """
        emf_record = {
            "_aws": {
                "Timestamp": int(datetime.now(tz=UTC).timestamp() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [list(self._default_dimensions.keys())],
                        "Metrics": [],
                    }
                ],
            },
            **self._default_dimensions,
            "level": record.get("level", {}).get("name", "INFO"),
            "message": record.get("message", ""),
            "module": record.get("name", ""),
            "timestamp": record.get("time", ""),
        }

        return json.dumps(emf_record).decode("utf-8")


class AmiraLogger:
    """Enhanced logger with correlation support and multiple output formats.

    Args:
        name: Logger name/identifier
        config: Logger configuration
    """

    def __init__(self, *, name: str, config: LoggerConfig) -> None:
        self._name = name
        self._config = config
        self._correlation_manager = CorrelationManager()
        self._setup_logger()

    def _setup_logger(self) -> None:
        """Configure loguru logger based on configuration."""
        _loguru_logger.remove()

        # Determine output stream
        output_stream = sys.stdout if self._config.use_stdout else sys.stderr

        # Create sampling filter if needed
        sampling_filter = (
            self._create_sampling_filter() if self._config.sampling_rate < 1.0 else None
        )

        # Combine filters
        filters = []
        if self._config.include_correlation:
            filters.append(self._correlation_filter)
        if sampling_filter:
            filters.append(sampling_filter)

        combined_filter = self._combine_filters(filters) if filters else None

        if self._config.format_type == LogFormat.EMF:
            _loguru_logger.add(
                output_stream,
                format=self._format_emf,
                level=self._config.level.value,
                filter=combined_filter,  # type: ignore[arg-type]
            )
        elif self._config.format_type == LogFormat.JSON:
            _loguru_logger.add(
                output_stream,
                level=self._config.level.value,
                filter=combined_filter,  # type: ignore[arg-type]
                serialize=True,
            )
        else:
            _loguru_logger.add(
                output_stream,
                format=self._get_format_string(),
                level=self._config.level.value,
                filter=combined_filter,  # type: ignore[arg-type]
                colorize=True,  # Always enable colors for better readability
            )

    def _create_sampling_filter(self) -> Callable[["Record"], bool]:
        """Create a sampling filter function.

        Returns:
            Filter function that samples logs based on configured rate
        """

        def _sample(record: "Record") -> bool:
            return random.random() < self._config.sampling_rate

        return _sample

    def _combine_filters(
        self, filters: list[Callable[["Record"], bool]]
    ) -> Callable[["Record"], bool]:
        """Combine multiple filter functions.

        Args:
            filters: List of filter functions to combine

        Returns:
            Combined filter function that applies all filters
        """

        def combined_filter(record: "Record") -> bool:
            return all(f(record) for f in filters)

        return combined_filter

    def _get_format_string(self) -> str:
        """Get format string based on configuration.

        Returns:
            Appropriate format string for the configured format type
        """
        # Include service in format if configured
        service_part = " | <blue>{extra[service]}</blue>" if self._config.service else ""
        correlation_part = (
            " | <yellow>{extra[correlation]}</yellow>" if self._config.include_correlation else ""
        )

        match self._config.format_type:
            case LogFormat.JSON:
                base = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}"
                if self._config.service:
                    base += " | {extra[service]}"
                if self._config.include_correlation:
                    base += " | {extra[correlation]}"
                return base + " | {message}"
            case LogFormat.EMF:
                return "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {message}"
            case _:
                return f"<green>{{time:HH:mm:ss.SSS}}</green> | <level>{{level: <8}}</level> | <cyan>{{name}}</cyan>:<cyan>{{function}}</cyan>:<cyan>{{line}}</cyan>{service_part}{correlation_part} - <level>{{message}}</level>"

    def _format_emf(self, record: "Record") -> str:
        """Format record as EMF JSON.

        Args:
            record: Log record to format

        Returns:
            EMF-formatted string
        """
        if not self._config.cloudwatch_namespace:
            return json.dumps(
                {
                    "timestamp": record["time"].isoformat(),
                    "level": record["level"].name,
                    "name": record["name"],
                    "message": record["message"],
                }
            )

        formatter = EMFFormatter(
            namespace=self._config.cloudwatch_namespace,
            default_dimensions=self._config.cloudwatch_dimensions,
        )
        return formatter.format_log(record=dict(record))

    def _correlation_filter(self, record: dict[str, Any]) -> bool:
        """Add correlation info and service context to log record.

        Args:
            record: Log record to enhance

        Returns:
            Always True to allow record through
        """
        correlation_info = self._correlation_manager.current
        record["extra"]["correlation"] = str(correlation_info) if correlation_info else ""

        # Add service context if configured
        if self._config.service:
            record["extra"]["service"] = self._config.service

        return True

    @contextmanager
    def correlation_context(
        self, *, description: str, request_id: str | None = None, activity_id: str | None = None
    ) -> Generator[CorrelationInfo, None, None]:
        """Context manager for correlation tracking.

        Args:
            description: Description of the operation
            request_id: Optional specific request ID
            activity_id: Optional activity ID for context

        Yields:
            Created correlation info
        """
        correlation_info = self._correlation_manager.create_new(
            request_id=request_id, activity_id=activity_id
        )
        _loguru_logger.opt(depth=1).info(
            f"Setting correlation id for '{description}' to {correlation_info}"
        )

        try:
            yield correlation_info
        finally:
            self._correlation_manager.clear()

    def create_propagating_wrapper(self, *, func: Callable[..., Any]) -> PropagatingCorrelation:
        """Create correlation-propagating wrapper for function.

        Args:
            func: Function to wrap

        Returns:
            Wrapped function that propagates correlation
        """
        return PropagatingCorrelation(func=func, correlation_manager=self._correlation_manager)

    @property
    def correlation_info(self) -> CorrelationInfo | None:
        """Get current correlation info."""
        return self._correlation_manager.current

    def set_correlation(self, *, correlation_info: CorrelationInfo) -> None:
        """Set correlation info for current context.

        Args:
            correlation_info: Correlation information to set
        """
        self._correlation_manager.set(correlation_info=correlation_info)

    def clear_correlation(self) -> None:
        """Clear current correlation info."""
        self._correlation_manager.clear()

    def set_activity_context(self, *, activity_id: str) -> None:
        """Set or update activity context for current correlation.

        Args:
            activity_id: Activity ID to add to context
        """
        current = self._correlation_manager.current
        if current:
            # Update existing correlation with activity ID
            updated = CorrelationInfo(request_id=current.request_id, activity_id=activity_id)
            self._correlation_manager.set(correlation_info=updated)
        else:
            # Create new correlation with activity ID
            self._correlation_manager.create_new(activity_id=activity_id)

    def trace(self, message: str, **kwargs: Any) -> None:
        """Log trace message."""
        _loguru_logger.opt(depth=1).trace(message, **kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message."""
        _loguru_logger.opt(depth=1).debug(message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message."""
        _loguru_logger.opt(depth=1).info(message, **kwargs)

    def success(self, message: str, **kwargs: Any) -> None:
        """Log success message."""
        _loguru_logger.opt(depth=1).success(message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message."""
        _loguru_logger.opt(depth=1).warning(message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message."""
        _loguru_logger.opt(depth=1).error(message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log critical message."""
        _loguru_logger.opt(depth=1).critical(message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log exception with traceback."""
        _loguru_logger.opt(depth=1).exception(message, **kwargs)


_loggers: dict[str, AmiraLogger] = {}
_default_config = LoggerConfig()

LOGLEVEL: Final[str] = os.getenv("LOGLEVEL", "INFO")
LOG_FORMAT: Final[str] = os.getenv("LOG_FORMAT", "standard")
CLOUDWATCH_NAMESPACE: Final[str | None] = os.getenv("CLOUDWATCH_NAMESPACE")


# Removed configure_default_logger - no longer needed with simplified API


def get_logger(
    name: str, *, service: str | None = None, config: LoggerConfig | None = None
) -> AmiraLogger:
    """Get or create logger instance - the ONE function you need for logging.

    Args:
        name: Logger name/identifier (use __name__ for module loggers)
        service: Optional service name to include in log context
        config: Optional specific configuration (rarely needed - auto-configures from environment)

    Returns:
        Configured logger instance

    Examples:
        # In most source files:
        logger = get_logger(__name__)

        # At application entry points with service context:
        logger = get_logger(__name__, service="my-app")

        # Environment variables automatically detected:
        # LOGLEVEL=DEBUG, LOG_JSON=true, AWS_LAMBDA_FUNCTION_NAME, etc.
    """
    # Create cache key that includes service context
    cache_key = f"{name}:{service}" if service else name

    if cache_key in _loggers:
        return _loggers[cache_key]

    effective_config = config or _default_config

    if not config and _default_config == LoggerConfig():
        # Auto-configure from environment if no explicit config
        level_env = os.getenv("LOGLEVEL", os.getenv("LOG_LEVEL", "INFO"))
        format_env = os.getenv("LOG_FORMAT", "standard")
        json_env = os.getenv("LOG_JSON", "false")

        # Auto-detect AWS environments
        is_lambda = os.getenv("AWS_LAMBDA_FUNCTION_NAME") is not None
        is_container = os.getenv("ECS_CONTAINER_METADATA_URI") is not None

        # Determine format based on environment and LOG_JSON setting
        if json_env == "false" and (is_lambda or is_container):
            json_env = "true"  # Auto-enable JSON in AWS environments

        serialize = json_env.lower() in {"1", "true", "yes", "on"}
        if serialize:
            format_type = LogFormat.JSON
        else:
            format_type = LogFormat(format_env)

        # Get sampling rate
        sampling_rate = _get_sampling_rate("LOG_SAMPLING_RATE", 1.0)

        effective_config = LoggerConfig(
            level=LogLevel(level_env.upper()),
            format_type=format_type,
            service=service,
            sampling_rate=sampling_rate,
            cloudwatch_namespace=CLOUDWATCH_NAMESPACE,
            use_stdout=True,  # Default to stdout for better visibility
        )

    logger_instance = AmiraLogger(name=name, config=effective_config)
    _loggers[cache_key] = logger_instance

    return logger_instance


def thread_ident_string() -> str:
    """Get thread identification string.

    Returns:
        String identifying current process and thread
    """
    return f"({os.getpid()}:{threading.get_ident()})"


def _get_sampling_rate(env_name: str, default: float) -> float:
    """Get sampling rate from environment variable.

    Args:
        env_name: Environment variable name
        default: Default value if parsing fails

    Returns:
        Sampling rate between 0.0 and 1.0
    """
    try:
        value = float(os.getenv(env_name, str(default)))
        if not (0.0 <= value <= 1.0):
            return default
        return value
    except Exception:
        return default


def emit_emf_metric(
    *,
    namespace: str,
    metrics: dict[str, float],
    dimensions: dict[str, str] | None = None,
    timestamp_ms: int | None = None,
    logger_name: str = "__main__",
) -> None:
    """Emit CloudWatch Embedded Metric Format JSON via logger for auto-ingestion.

    Args:
        namespace: CloudWatch metrics namespace
        metrics: Map of metric name to float value
        dimensions: Optional dimensions key-value map
        timestamp_ms: Optional epoch ms; default now
        logger_name: Logger name to use for emission
    """
    sampling_rate: float = _get_sampling_rate("EMF_SAMPLING_RATE", 1.0)
    if sampling_rate < 1.0 and random.random() > sampling_rate:
        return

    ts: int = timestamp_ms or int(time.time() * 1000)
    dims = dimensions or {}

    emf: dict[str, Any] = {
        "_aws": {
            "Timestamp": ts,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dims.keys())] if dims else [[]],
                    "Metrics": [{"Name": k, "Unit": "None"} for k in metrics.keys()],
                }
            ],
        }
    }

    payload: dict[str, Any] = {**emf, **metrics, **dims}
    logger = get_logger(logger_name)
    logger.info(json.dumps(payload))


# Backward compatibility alias - just use get_logger instead
def setup_logging(*, service: str | None = None) -> AmiraLogger:
    """Backward compatibility function - use get_logger() instead.

    Args:
        service: Optional service name (will be ignored - use get_logger with service config instead)

    Returns:
        Main logger instance
    """
    import warnings

    warnings.warn(
        "setup_logging() is deprecated. Use get_logger(__name__) instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_logger("__main__")
