"""Configuration constants for ORF rescoring pipeline.

This module provides centralized access to configuration values through
environment variable loading and constant definitions. It loads the .env
file once at module import and exposes all configuration as typed constants.
"""

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

_PROJECT_ROOT: Final[Path] = Path(__file__).parent.parent
_ENV_FILE: Final[Path] = _PROJECT_ROOT / ".env"

load_dotenv(_ENV_FILE)

DEEPGRAM_API_KEY: Final[str] = os.getenv("DEEPGRAM_API_KEY", "")

SLACK_WEBHOOK_URL: Final[str | None] = os.getenv("SLACK_WEBHOOK_URL")

SPANISH_GSHEET_1: Final[str | None] = os.getenv("SPANISH_GSHEET_1")
SPANISH_GSHEET_2: Final[str | None] = os.getenv("SPANISH_GSHEET_2")

AMIRA_PYUTILS_ASSETS_ROOT: Final[str | None] = os.getenv("AMIRA_PYUTILS_ASSETS_ROOT")

DEFAULT_DATABASE: Final[str] = "production_amira_datalake"
DEFAULT_REGION: Final[str] = "us-east-2"
DEFAULT_S3_STAGING_BUCKET: Final[str] = "amira-latent-space"
DEFAULT_S3_STAGING_PATH: Final[str] = "queries"

EDM_CORRECT_THRESHOLD: Final[int] = 2

DEFAULT_LOG_LEVEL: Final[str] = "DEBUG"
DEFAULT_LOG_FILE: Final[str] = "pipeline.log"
