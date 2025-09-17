from typing import Final, Callable, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Record
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import os
import threading
import uuid
import sys
from datetime import datetime, timezone

from loguru import logger as _loguru_logger
import orjson as json


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
    """

    request_id: str

    def __post_init__(self) -> None:
        if not self.request_id:
            object.__setattr__(self, "request_id", uuid.uuid4().hex)

    def __str__(self) -> str:
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
    """

    level: LogLevel = LogLevel.INFO
    format_type: LogFormat = LogFormat.STANDARD
    include_correlation: bool = True
    cloudwatch_namespace: str | None = None
    cloudwatch_dimensions: dict[str, str] | None = None


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

    def create_new(self, *, request_id: str | None = None) -> CorrelationInfo:
        """Create and set new correlation info.

        Args:
            request_id: Optional specific request ID to use

        Returns:
            Created correlation info
        """
        correlation_info = CorrelationInfo(request_id=request_id or uuid.uuid4().hex)
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
                "Timestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
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

        correlation_filter = self._correlation_filter if self._config.include_correlation else None

        if self._config.format_type == LogFormat.EMF:
            _loguru_logger.add(
                sys.stderr,
                format=self._format_emf,
                level=self._config.level.value,
                filter=correlation_filter,  # type: ignore[arg-type]
            )
        elif self._config.format_type == LogFormat.JSON:
            _loguru_logger.add(
                sys.stderr,
                level=self._config.level.value,
                filter=correlation_filter,  # type: ignore[arg-type]
                serialize=True,
            )
        else:
            _loguru_logger.add(
                sys.stderr,
                format=self._get_format_string(),
                level=self._config.level.value,
                filter=correlation_filter,  # type: ignore[arg-type]
            )

    def _get_format_string(self) -> str:
        """Get format string based on configuration.

        Returns:
            Appropriate format string for the configured format type
        """
        match self._config.format_type:
            case LogFormat.JSON:
                return "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {extra[correlation]} | {message}"
            case LogFormat.EMF:
                return "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {message}"
            case _:
                return "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> | <yellow>{extra[correlation]}</yellow> | {message}"

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
            ).decode("utf-8")

        formatter = EMFFormatter(
            namespace=self._config.cloudwatch_namespace,
            default_dimensions=self._config.cloudwatch_dimensions,
        )
        return formatter.format_log(record=dict(record))

    def _correlation_filter(self, record: dict[str, Any]) -> bool:
        """Add correlation info to log record.

        Args:
            record: Log record to enhance

        Returns:
            Always True to allow record through
        """
        correlation_info = self._correlation_manager.current
        record["extra"]["correlation"] = str(correlation_info) if correlation_info else ""
        return True

    @contextmanager
    def correlation_context(
        self, *, description: str, request_id: str | None = None
    ) -> Generator[CorrelationInfo, None, None]:
        """Context manager for correlation tracking.

        Args:
            description: Description of the operation
            request_id: Optional specific request ID

        Yields:
            Created correlation info
        """
        correlation_info = self._correlation_manager.create_new(request_id=request_id)
        _loguru_logger.info(
            f"Setting correlation id for '{description}' to {correlation_info.request_id}"
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

    def trace(self, message: str, **kwargs: Any) -> None:
        """Log trace message."""
        _loguru_logger.trace(message, **kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message."""
        _loguru_logger.debug(message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message."""
        _loguru_logger.info(message, **kwargs)

    def success(self, message: str, **kwargs: Any) -> None:
        """Log success message."""
        _loguru_logger.success(message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message."""
        _loguru_logger.warning(message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message."""
        _loguru_logger.error(message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log critical message."""
        _loguru_logger.critical(message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log exception with traceback."""
        _loguru_logger.exception(message, **kwargs)


_loggers: dict[str, AmiraLogger] = {}
_default_config = LoggerConfig()

LOGLEVEL: Final[str] = os.getenv("LOGLEVEL", "INFO")
LOG_FORMAT: Final[str] = os.getenv("LOG_FORMAT", "standard")
CLOUDWATCH_NAMESPACE: Final[str | None] = os.getenv("CLOUDWATCH_NAMESPACE")


def configure_default_logger(*, config: LoggerConfig) -> None:
    """Configure default logger settings.

    Args:
        config: Logger configuration to use as default
    """
    global _default_config
    _default_config = config
    _loggers.clear()


def get_logger(name: str, config: LoggerConfig | None = None) -> AmiraLogger:
    """Get or create logger instance.

    Args:
        name: Logger name/identifier
        config: Optional specific configuration for this logger

    Returns:
        Configured logger instance
    """
    if name in _loggers:
        return _loggers[name]

    effective_config = config or _default_config

    if not config:
        effective_config = LoggerConfig(
            level=LogLevel(LOGLEVEL),
            format_type=LogFormat(LOG_FORMAT),
            cloudwatch_namespace=CLOUDWATCH_NAMESPACE,
        )

    logger_instance = AmiraLogger(name=name, config=effective_config)
    _loggers[name] = logger_instance

    return logger_instance


def thread_ident_string() -> str:
    """Get thread identification string.

    Returns:
        String identifying current process and thread
    """
    return f"({os.getpid()}:{threading.get_ident()})"
