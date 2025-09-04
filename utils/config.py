import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

from src.pipeline.config_validator import ConfigurationValidator
from src.pipeline.exceptions import ConfigurationError
from src.pipeline.inference.models import W2VConfig

DEFAULT_CONFIG_PATH: Final[str] = "config_parallel.yaml"
DEFAULT_RESULT_DIR: Final[str] = "2025_letter_sound_scoring"
LOG_FORMAT: Final[str] = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"

# S3 Audio Configuration Constants
S3_SPEECH_ROOT_PROD: Final[str] = "amira-speech-stream"
S3_SPEECH_ROOT_STAGE: Final[str] = "amira-speech-stream-stage"
RECONSTITUTED_PHRASE_AUDIO: Final[str] = "reconstituted_phrase_audio"


if os.getenv("ENABLE_FILE_LOG", "0") == "1":
    logger.add("pipeline_execution.log")


class PipelineMetadataConfig(BaseModel):
    """Pipeline metadata configuration.

    Either provide an explicit date range via ``processing_start_time`` and
    ``processing_end_time`` or set the ``PROCESSING_HOURS_AGO`` environment
    variable to derive a rolling window. Implicit defaults are not applied.
    """

    processing_start_time: datetime | None = None
    processing_end_time: datetime | None = None
    activity_file: str | None = None
    activity_id: str | None = None
    limit: int = 5
    correlation_id: str | None = None


class ResultConfig(BaseModel):
    """Result configuration."""

    output_dir: str = DEFAULT_RESULT_DIR
    audit_mode: bool = True


class CachedConfig(BaseModel):
    """Cached configuration."""

    story_phrase_path: Path = Path("data/letter_sound_story_phrase.csv")


class AudioConfig(BaseModel):
    """Audio processing configuration."""

    audio_dir: Path = Path("audio")
    save_padded_audio: bool = True
    padded_seconds: int = 3
    use_complete_audio: bool = False

    @field_validator("padded_seconds")
    @classmethod
    def validate_padded_seconds(cls, v: int) -> int:
        """Validate padded_seconds is within reasonable range."""
        if v < 0 or v > 30:
            raise ValueError("padded_seconds must be between 0 and 30")
        return v


class QueueSizesConfig(BaseModel):
    """Queue sizes configuration."""

    audio_queue: int = 100
    transcription_queue: int = 80
    status_updates_queue: int = 400

    @field_validator("audio_queue", "transcription_queue", "status_updates_queue")
    @classmethod
    def validate_positive_queue_size(cls, v: int) -> int:
        """Validate queue sizes are positive."""
        if v <= 0:
            raise ValueError("Queue sizes must be positive integers")
        return v


class TimeoutsConfig(BaseModel):
    """Timeout configuration."""

    audio_queue_put_timeout: int = 60
    transcription_queue_put_timeout: int = 90
    transcription_queue_get_timeout: int = 15


class AwsConfig(BaseModel):
    """AWS configuration."""

    region: str = "us-east-2"
    aws_region: str = "us-east-2"
    athena_schema: str = "production_amira_datalake"
    s3_bucket: str = "production-amira-datalake"
    athena_s3_staging_dir: str = "athena"
    audio_env: str = "prod2"
    appsync_env: str = "prod2"
    aws_profile: str = "legacy"

    @model_validator(mode="after")
    def normalize_region(self) -> "AwsConfig":
        """Ensure ``aws_region`` is the source of truth and keep ``region`` in sync."""

        primary: str = (self.aws_region or "").strip() or (self.region or "").strip()
        self.aws_region = primary or "us-east-2"
        self.region = self.aws_region
        return self


class PipelineConfig(BaseModel):
    """Complete pipeline configuration."""

    metadata: PipelineMetadataConfig = Field(default_factory=PipelineMetadataConfig)
    result: ResultConfig = Field(default_factory=ResultConfig)
    cached: CachedConfig = Field(default_factory=CachedConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    w2v2: W2VConfig = Field(default_factory=W2VConfig)
    aws: AwsConfig = Field(default_factory=AwsConfig)
    phrase_to_align: list[int] = Field(default_factory=lambda: [4, 11])
    enable_confidence_weighting: bool = False

    @model_validator(mode="after")
    def validate_config_consistency(self) -> "PipelineConfig":
        """Validate configuration consistency across fields without implicit defaults."""

        start = self.metadata.processing_start_time
        end = self.metadata.processing_end_time

        if (start is None) ^ (end is None):
            raise ValueError(
                "Both processing_start_time and processing_end_time must be set together, "
                "or omit both and set PROCESSING_HOURS_AGO."
            )

        if start is not None and end is not None and start >= end:
            raise ValueError("processing_start_time must be before processing_end_time")
        return self

    def validate_runtime_requirements(self) -> None:
        """Validate runtime requirements and dependencies.

        This method should be called after configuration is loaded
        to ensure all required directories and dependencies are available.

        Raises:
            ValueError: If validation fails
        """
        try:
            self.audio.audio_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise ValueError(f"Cannot create audio directory {self.audio.audio_dir}: {e}")

        if not self.cached.story_phrase_path.exists():
            logger.warning(f"Story phrase file not found: {self.cached.story_phrase_path}")

        if self.w2v2.use_triton:
            if not self.w2v2.triton_url or not self.w2v2.triton_url.strip():
                raise ValueError("triton_url is required when use_triton is True")

        logger.debug("Configuration runtime validation passed")


def load_config(*, config_path: str | None = None) -> PipelineConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        Configuration object loaded from YAML file.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        yaml.YAMLError: If YAML parsing fails.
    """
    if config_path is None:
        config = PipelineConfig()
    else:
        config_file: Path = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        try:
            with config_file.open("r") as file:
                raw_config: dict[str, Any] = yaml.safe_load(file)
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Failed to parse YAML config: {e}") from e

        try:
            config = PipelineConfig(**raw_config)
        except Exception as e:
            raise ValueError(f"Invalid configuration: {e}") from e

        logger.info(f"Config loaded from {config_path}")

    # Override with environment variables if present (for containerized deployment)
    if os.getenv("USE_TRITON"):
        config.w2v2.use_triton = os.getenv("USE_TRITON", "false").lower() == "true"
    if os.getenv("TRITON_URL"):
        config.w2v2.triton_url = os.getenv("TRITON_URL", config.w2v2.triton_url)
    if os.getenv("TRITON_MODEL"):
        config.w2v2.triton_model = os.getenv("TRITON_MODEL", config.w2v2.triton_model)
    if os.getenv("MODEL_PATH"):
        config.w2v2.model_path = os.getenv("MODEL_PATH", config.w2v2.model_path)
    if os.getenv("AWS_PROFILE"):
        config.aws.aws_profile = os.getenv("AWS_PROFILE", config.aws.aws_profile)
    if os.getenv("AWS_REGION"):
        region_env = os.getenv("AWS_REGION", config.aws.aws_region)
        config.aws.aws_region = region_env
        config.aws.region = region_env
    if os.getenv("ALIGNER_CONFIDENCE_WEIGHTING"):
        config.enable_confidence_weighting = os.getenv(
            "ALIGNER_CONFIDENCE_WEIGHTING", "false"
        ).lower() in {"1", "true", "yes", "on"}

    if os.getenv("PROCESSING_START_TIME"):
        try:
            from dateutil import parser  # type: ignore[import-untyped]

            config.metadata.processing_start_time = parser.parse(os.getenv("PROCESSING_START_TIME"))
        except Exception as e:
            logger.warning(f"Failed to parse PROCESSING_START_TIME: {e}")

    if os.getenv("PROCESSING_END_TIME"):
        try:
            config.metadata.processing_end_time = parser.parse(os.getenv("PROCESSING_END_TIME"))
        except Exception as e:
            logger.warning(f"Failed to parse PROCESSING_END_TIME: {e}")

    if os.getenv("PROCESSING_HOURS_AGO"):
        try:
            hours_ago = int(os.getenv("PROCESSING_HOURS_AGO", "0"))
            config.metadata.processing_end_time = datetime.now()
            config.metadata.processing_start_time = config.metadata.processing_end_time - timedelta(
                hours=hours_ago
            )
            logger.info(
                f"Using PROCESSING_HOURS_AGO={hours_ago}: {config.metadata.processing_start_time} to {config.metadata.processing_end_time}"
            )
        except Exception as e:
            logger.warning(f"Failed to parse PROCESSING_HOURS_AGO: {e}")

    try:
        config.validate_runtime_requirements()
        ConfigurationValidator.validate_full_configuration(config)
    except ConfigurationError as e:
        logger.error(f"Configuration validation failed: {e}")
        raise
    except Exception as e:
        logger.warning(f"Non-critical configuration validation warning: {e}")

    return config
