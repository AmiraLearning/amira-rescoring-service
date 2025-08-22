from typing import Final, Any
from pydantic import BaseModel, Field
from pathlib import Path
import yaml
from loguru import logger
from src.pipeline.inference.constants import DeviceType
from datetime import datetime


DEFAULT_CONFIG_PATH: Final[str] = "config_parallel.yaml"
DEFAULT_RESULT_DIR: Final[str] = "2025_letter_sound_scoring"
LOG_FORMAT: Final[str] = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"


logger.add("pipeline_execution.log")


class PipelineMetadataConfig(BaseModel):
    """Pipeline metadata configuration."""

    processing_start_time: datetime = datetime(2025, 1, 9, 0, 0, 0)
    processing_end_time: datetime = datetime(2025, 1, 10, 23, 59, 59)
    activity_file: str | None = None
    activity_id: str | None = None
    limit: int = 5


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


class QueueSizesConfig(BaseModel):
    """Queue sizes configuration."""

    audio_queue: int = 100
    transcription_queue: int = 80
    status_updates_queue: int = 400


class TimeoutsConfig(BaseModel):
    """Timeout configuration."""

    audio_queue_put_timeout: int = 60
    transcription_queue_put_timeout: int = 90
    transcription_queue_get_timeout: int = 15


class AwsConfig(BaseModel):
    """AWS configuration."""

    region: str = "us-east-2"
    aws_region: str = "us-east-2"  # Alias for region
    athena_schema: str = "production_amira_datalake"
    s3_bucket: str = "production-amira-datalake"
    athena_s3_staging_dir: str = "athena"
    audio_env: str = "prod2"
    appsync_env: str = "prod2"
    aws_profile: str = "legacy"


class W2VConfig(BaseModel):
    """Wav2Vec2 configuration."""

    model_path: str = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"
    device: DeviceType = DeviceType.CPU
    include_confidence: bool = False


class PipelineConfig(BaseModel):
    """Complete pipeline configuration."""

    metadata: PipelineMetadataConfig = Field(default_factory=PipelineMetadataConfig)
    result: ResultConfig = Field(default_factory=ResultConfig)
    cached: CachedConfig = Field(default_factory=CachedConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    w2v2: W2VConfig = Field(default_factory=W2VConfig)
    aws: AwsConfig = Field(default_factory=AwsConfig)
    phrase_to_align: list[int] = Field(default_factory=lambda: [4, 11])


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
        return PipelineConfig()

    config_file: Path = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with config_file.open("r") as file:
            raw_config: dict[str, Any] = yaml.safe_load(file)
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Failed to parse YAML config: {e}") from e

    try:
        config: PipelineConfig = PipelineConfig(**raw_config)
    except Exception as e:
        raise ValueError(f"Invalid configuration: {e}") from e

    logger.info(f"Config loaded from {config_path}")
    return config
