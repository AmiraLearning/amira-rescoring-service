import logging
from pathlib import Path
from typing import Final

EXTERNAL_LOGGERS: Final[list[str]] = [
    "boto3",
    "botocore",
    "urllib3",
    "requests",
    "amira_pyutils",
    "pyutils-amira_pyutils",
    "s3transfer",
    "botocore.credentials",
    "botocore.utils",
    "botocore.hooks",
    "botocore.loaders",
    "botocore.parsers",
    "botocore.endpoint",
    "botocore.auth",
    "botocore.handlers",
    "botocore.httpsession",
    "botocore.regions",
    "botocore.client",
    "botocore.resources",
    "botocore.awsrequest",
]


def configure_logging(
    *, log_level: str = "INFO", log_file: str = "pipeline.log", quiet: bool = False
) -> None:
    """Configure logging for the pipeline.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
        log_file: Path to log file.
        quiet: If True, suppress console output for parallel processing.
    """
    detailed_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    pipeline_dir = Path(__file__).parent.parent
    log_path = pipeline_dir / log_file

    handlers: list[logging.Handler] = [logging.FileHandler(str(log_path))]

    if not quiet:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(detailed_formatter)
        handlers.append(console_handler)

    handlers[0].setFormatter(detailed_formatter)

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        handlers=handlers,
        force=True,
    )

    if quiet:
        _suppress_external_loggers(
            log_path=log_path, formatter=detailed_formatter, log_level=log_level
        )


def _suppress_external_loggers(
    *, log_path: Path, formatter: logging.Formatter, log_level: str
) -> None:
    """Suppress external library loggers in quiet mode.

    Args:
        log_path: Path to the log file.
        formatter: Logging formatter to use.
        log_level: Base logging level for the pipeline.
    """
    for logger_name in EXTERNAL_LOGGERS:
        ext_logger = logging.getLogger(logger_name)
        ext_logger.handlers.clear()
        ext_logger.setLevel(logging.WARNING)
        ext_logger.propagate = False

        file_handler = logging.FileHandler(str(log_path))
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.WARNING)
        ext_logger.addHandler(file_handler)

    _configure_s3_logger(log_path=log_path, formatter=formatter, log_level=log_level)
    _configure_timing_logger()
    _configure_boto_loggers()


def _configure_s3_logger(*, log_path: Path, formatter: logging.Formatter, log_level: str) -> None:
    """Configure S3 logger with special handling.

    Args:
        log_path: Path to the log file.
        formatter: Logging formatter to use.
        log_level: Base logging level for the pipeline.
    """
    s3_logger = logging.getLogger("pyutils-amira_pyutils.services.s3")
    s3_logger.handlers.clear()
    s3_logger.setLevel(logging.WARNING)
    s3_logger.propagate = False

    s3_file_handler = logging.FileHandler(str(log_path))
    s3_file_handler.setFormatter(formatter)
    s3_logger.addHandler(s3_file_handler)


def _configure_timing_logger() -> None:
    """Configure timing logger to suppress output."""
    timing_logger = logging.getLogger("amira_pyutils.general.timing")
    timing_logger.setLevel(logging.ERROR)
    timing_logger.propagate = False
    timing_logger.handlers.clear()


def _configure_boto_loggers() -> None:
    """Configure boto library loggers to WARNING level."""
    boto_loggers = ["boto", "botocore", "boto3"]
    for logger_name in boto_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
