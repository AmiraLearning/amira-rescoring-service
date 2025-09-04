"""Application-wide constants.

This module centralizes commonly used constants to avoid magic numbers throughout the codebase.
"""

from typing import Final

# Lambda and processing constants
DEFAULT_MAX_CONCURRENCY: Final[int] = 8
ENGINE_CACHE_MAX_DEFAULT: Final[int] = 2
MAX_CACHE_SIZE_DEFAULT: Final[int] = 32

# API and secrets constants
MIN_API_KEY_LENGTH: Final[int] = 10
DEFAULT_REGION: Final[str] = "us-east-1"
APPSYNC_SECRET_PREFIX: Final[str] = "amira/appsync"

# Audio processing constants
DEFAULT_SAMPLE_RATE: Final[int] = 16000
DEFAULT_TORCH_THREADS: Final[int] = 6
DEFAULT_INTEROP_THREADS: Final[int] = 2
MIN_AUDIO_SAMPLES_DEFAULT: Final[int] = 160  # 0.01 seconds at 16kHz
MAX_AUDIO_SAMPLES_DEFAULT: Final[int] = 16000000  # ~1000 seconds at 16kHz

# Timeout constants
DEFAULT_REQUEST_TIMEOUT: Final[int] = 10
CLOUDWATCH_TIMEOUT: Final[int] = 30
