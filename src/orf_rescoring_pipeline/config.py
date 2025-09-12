"""Configuration for ORF rescoring pipeline.

Provides a typed configuration model with environment-backed defaults and
an accessor that caches the loaded configuration for reuse.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Final

from pydantic import BaseModel, Field


class ORFConfig(BaseModel):
    """Pydantic configuration model for ORF rescoring pipeline."""

    pipeline_max_concurrency: Annotated[int, Field(ge=1, le=512)] = 10
    pipeline_inner_concurrency: Annotated[int, Field(ge=1, le=256)] = 8
    s3_warm_clients: Annotated[int, Field(ge=1, le=64)] = 2

    log_level: str = "DEBUG"
    debug: bool = False

    @classmethod
    def from_env(cls) -> ORFConfig:
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (ValueError, TypeError):
                return default

        def _bool(name: str, default: bool) -> bool:
            v = os.getenv(name)
            if v is None:
                return default
            return v.lower() in {"1", "true", "yes", "on"}

        return cls(
            pipeline_max_concurrency=_int("PIPELINE_MAX_CONCURRENCY", 10),
            pipeline_inner_concurrency=_int("PIPELINE_INNER_CONCURRENCY", 8),
            s3_warm_clients=_int("S3_WARM_CLIENTS", 2),
            log_level=os.getenv("LOG_LEVEL", "DEBUG"),
            debug=_bool("DEBUG", False),
        )


@lru_cache(maxsize=1)
def get_orf_config() -> ORFConfig:
    """Load and cache the ORF configuration from environment."""
    return ORFConfig.from_env()


DEFAULT_NS: Final[str] = "Amira/ORFRescore"
