import json
import os
import random
import sys
import time
from typing import Any, Final

from loguru import logger


def setup_logging(*, service: str | None = None) -> None:
    """Configure Loguru logger based on environment variables.

    Args:
        service: Optional service name to include in log context.
    """
    logger.remove()

    level_env: str = os.getenv("LOG_LEVEL", "INFO")

    is_lambda = os.getenv("AWS_LAMBDA_FUNCTION_NAME") is not None
    is_container = os.getenv("ECS_CONTAINER_METADATA_URI") is not None

    default_json = "true" if (is_lambda or is_container) else "false"
    json_env: str = os.getenv("LOG_JSON", default_json)
    serialize: bool = json_env.lower() in {"1", "true", "yes", "on"}

    if serialize:
        logger.add(sys.stdout, level=level_env, serialize=True)
    else:
        fmt: Final[str] = (
            "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
            if not service
            else "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <blue>{extra[service]}</blue> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )
        logger.add(sys.stdout, level=level_env, format=fmt, colorize=True)

    if service:
        logger.configure(extra={"service": service})

    try:
        sampling_rate_env: str = os.getenv("LOG_SAMPLING_RATE", "1.0")
        sampling_rate: float = max(0.0, min(1.0, float(sampling_rate_env)))
        if sampling_rate < 1.0:
            import random

            from loguru import Record

            def _sample(record: Record) -> bool:
                return random.random() < sampling_rate

            logger.add(sys.stdout, filter=_sample)
    except Exception:
        pass


def _get_sampling_rate(env_name: str, default: float) -> float:
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
) -> None:
    """Emit CloudWatch Embedded Metric Format JSON via stdout for auto-ingestion.

    Args:
        namespace: CloudWatch metrics namespace
        metrics: Map of metric name to float value
        dimensions: Optional dimensions key-value map
        timestamp_ms: Optional epoch ms; default now
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
    logger.info(json.dumps(payload))
