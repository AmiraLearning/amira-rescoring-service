"""Configuration validation module with comprehensive checks."""

import os
from pathlib import Path
from typing import Any

from loguru import logger

from src.pipeline.exceptions import ConfigurationError


class ConfigurationValidator:
    """Validates pipeline configuration at startup."""

    @staticmethod
    def validate_environment_variables() -> None:
        """Validate required environment variables are set.

        Raises:
            ConfigurationError: If required environment variables are missing or invalid.
        """
        critical_env_vars: list[str] = []
        optional_env_vars = [
            ("AWS_REGION", "us-east-2"),
            ("AWS_PROFILE", "legacy"),
            ("MAX_ACTIVITY_CONCURRENCY", "4"),
            ("MAX_CONCURRENT_DOWNLOADS", "64"),
            ("MAX_CONCURRENT_UPLOADS", "32"),
        ]

        # Check critical variables (none currently required)
        missing_critical = []
        for var in critical_env_vars:
            if not os.getenv(var):
                missing_critical.append(var)

        if missing_critical:
            raise ConfigurationError(
                f"Missing required environment variables: {', '.join(missing_critical)}"
            )

        # Warn about optional variables using defaults
        for var, default in optional_env_vars:
            if not os.getenv(var):
                logger.debug(f"Environment variable {var} not set, using default: {default}")

    @staticmethod
    def validate_aws_configuration(config: Any) -> None:
        """Validate AWS configuration settings.

        Args:
            config: PipelineConfig object with AWS settings.

        Raises:
            ConfigurationError: If AWS configuration is invalid.
        """
        if not hasattr(config, "aws"):
            raise ConfigurationError("Missing AWS configuration section")

        aws = config.aws

        # Validate region
        if not aws.aws_region or not aws.aws_region.strip():
            raise ConfigurationError("AWS region must be specified")

        # Validate S3 bucket
        if not aws.s3_bucket or not aws.s3_bucket.strip():
            raise ConfigurationError("S3 bucket must be specified")

        # Validate Athena settings
        if not aws.athena_schema or not aws.athena_schema.strip():
            raise ConfigurationError("Athena schema must be specified")

        logger.debug(
            f"AWS configuration validated: region={aws.aws_region}, bucket={aws.s3_bucket}"
        )

    @staticmethod
    def validate_inference_configuration(config: Any) -> None:
        """Validate inference engine configuration.

        Args:
            config: PipelineConfig object with W2V settings.

        Raises:
            ConfigurationError: If inference configuration is invalid.
        """
        if not hasattr(config, "w2v2"):
            raise ConfigurationError("Missing W2V2 configuration section")

        w2v = config.w2v2

        if w2v.use_triton:
            # Validate Triton-specific settings
            if not w2v.triton_url or not w2v.triton_url.strip():
                raise ConfigurationError("triton_url is required when use_triton is True")

            if not w2v.triton_url.startswith("https://"):
                raise ConfigurationError("triton_url must use HTTPS protocol")

            if not w2v.triton_model or not w2v.triton_model.strip():
                raise ConfigurationError("triton_model is required when use_triton is True")

            logger.info(
                f"Triton configuration validated: {w2v.triton_url} with model {w2v.triton_model}"
            )
        else:
            # Validate local model settings
            if not w2v.model_path or not w2v.model_path.strip():
                raise ConfigurationError("model_path is required when use_triton is False")

            # Check if model path exists (warning only)
            model_path = Path(w2v.model_path)
            if not model_path.exists():
                logger.warning(f"Model path does not exist: {w2v.model_path}")

            logger.info(f"Local model configuration validated: {w2v.model_path}")

    @staticmethod
    def validate_audio_configuration(config: Any) -> None:
        """Validate audio processing configuration.

        Args:
            config: PipelineConfig object with audio settings.

        Raises:
            ConfigurationError: If audio configuration is invalid.
        """
        if not hasattr(config, "audio"):
            raise ConfigurationError("Missing audio configuration section")

        audio = config.audio

        # Validate audio directory
        try:
            audio.audio_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise ConfigurationError(f"Cannot create audio directory {audio.audio_dir}: {e}")

        # Validate padded seconds range
        if audio.padded_seconds < 0 or audio.padded_seconds > 30:
            raise ConfigurationError("padded_seconds must be between 0 and 30")

        logger.debug(
            f"Audio configuration validated: dir={audio.audio_dir}, padding={audio.padded_seconds}s"
        )

    @staticmethod
    def validate_metadata_configuration(config: Any) -> None:
        """Validate metadata configuration including date ranges.

        Args:
            config: PipelineConfig object with metadata settings.

        Raises:
            ConfigurationError: If metadata configuration is invalid.
        """
        if not hasattr(config, "metadata"):
            logger.warning("Missing metadata configuration section, using defaults")
            return

        metadata = config.metadata

        # Validate date presence and range
        start = metadata.processing_start_time
        end = metadata.processing_end_time

        if start is None and end is None:
            logger.debug("No processing date range provided; upstream may derive it via hours-ago.")
            return
        if (start is None) ^ (end is None):
            raise ConfigurationError(
                "Both processing_start_time and processing_end_time must be set together"
            )
        if start is not None and end is not None and start >= end:
            raise ConfigurationError("processing_start_time must be before processing_end_time")

        # Validate limit
        if metadata.limit <= 0:
            raise ConfigurationError("limit must be a positive integer")

        # Warn if using default dates (likely hardcoded)
        from datetime import datetime

        default_start = datetime(2025, 1, 9, 0, 0, 0)
        default_end = datetime(2025, 1, 10, 23, 59, 59)

        if (
            metadata.processing_start_time == default_start
            and metadata.processing_end_time == default_end
        ):
            logger.warning(
                "Using default hardcoded date range (2025-01-09 to 2025-01-10). "
                "Consider setting explicit dates via configuration or environment variables."
            )

        logger.debug(
            f"Metadata configuration validated: {metadata.processing_start_time} to {metadata.processing_end_time}"
        )

    @staticmethod
    def validate_full_configuration(config: Any) -> None:
        """Perform comprehensive configuration validation.

        Args:
            config: PipelineConfig object to validate.

        Raises:
            ConfigurationError: If any configuration is invalid.
        """
        logger.info("Starting configuration validation...")

        try:
            ConfigurationValidator.validate_environment_variables()
            ConfigurationValidator.validate_aws_configuration(config)
            ConfigurationValidator.validate_inference_configuration(config)
            ConfigurationValidator.validate_audio_configuration(config)
            ConfigurationValidator.validate_metadata_configuration(config)

            # Additional cross-configuration validations
            if hasattr(config, "cached") and config.cached.story_phrase_path:
                if not config.cached.story_phrase_path.exists():
                    logger.warning(
                        f"Story phrase file not found: {config.cached.story_phrase_path}"
                    )

            logger.info("Configuration validation completed successfully")

        except ConfigurationError:
            raise
        except Exception as e:
            raise ConfigurationError(f"Unexpected error during configuration validation: {e}")
