from datetime import datetime, timedelta
from pathlib import Path
from enum import StrEnum
from typing import Final
from types import MappingProxyType

from loguru import logger
import polars as pl

from utils.config import PipelineConfig
from infra.athena_client import query_athena

from ast import literal_eval

ACTIVITY_QUERY: Final[str] = """
    SELECT activityid,
        storyid,
        studentid,
        districtid,
        type,
        createdat,
        status,
        displaystatus
    FROM activities_v
    WHERE type = 'letterNamingAndSounds'
    AND createdat >= TIMESTAMP '{start_time}'
    AND createdat < TIMESTAMP '{end_time}'
    AND districtid in (955168, 1000022968)
    AND status = 'underReview' 
"""

COLUMN_MAPPING: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "activityid": "activityId",
        "storyid": "storyId",
        "studentid": "studentId",
        "districtid": "districtId",
        "schoolid": "schoolId",
        "createdat": "createdAt",
        "status": "status",
        "displaystatus": "displayStatus",
    }
)


class FileFormat(StrEnum):
    """File format enum."""

    PARQUET = ".parquet"
    CSV = ".csv"


class StoryPhraseColumns(StrEnum):
    """Columns for story phrase data."""

    EXPECTED_TEXT = "expected_text"
    REFERENCE_PHONEMES = "reference_phonemes"


class MergeColumns(StrEnum):
    """Columns for merging activities with phrases."""

    STORY_ID = "storyId"
    PHRASE_INDEX = "phraseIndex"


async def load_activity_data(*, config: PipelineConfig) -> pl.DataFrame:
    """Fetch activity data based on config and save activities.csv.

    Args:
        config: Pipeline configuration containing metadata and result settings.

    Returns:
        DataFrame with activity data.

    Raises:
        ValueError: If no activities found for the specified date range.
    """
    result_dir: str = config.result.output_dir

    if config.metadata.activity_file:
        logger.info(
            f"Loading existing activity data from {config.metadata.activity_file}"
        )
        activity_df: pl.DataFrame = pl.read_csv(config.metadata.activity_file)
    else:
        logger.info("Fetching activities from data lake")
        processing_start_time: str | None = config.metadata.processing_start_time
        processing_end_time: str | None = config.metadata.processing_end_time

        if not processing_start_time or not processing_end_time:
            yesterday: datetime = datetime.now() - timedelta(days=1)
            processing_start_time = yesterday.strftime("%Y-%m-%d 00:00:00")
            processing_end_time = yesterday.strftime("%Y-%m-%d 23:59:59")
            logger.info(
                f"Using default time range: {processing_start_time} to {processing_end_time}"
            )

        activity_df: pl.DataFrame = await query_athena(
            query=ACTIVITY_QUERY,
            start_time=processing_start_time,
            end_time=processing_end_time,
        )

        logger.info(f"Fetched {activity_df.height} activities")
        if activity_df.is_empty():
            raise ValueError("No activities found for the specified date range")

        activity_df = activity_df.rename(columns=COLUMN_MAPPING)

        activity_raw_path: Path = Path(result_dir) / "activities.csv"
        activity_df.write_csv(activity_raw_path)
        logger.info(f"Activity raw data saved: {activity_raw_path}")

    return activity_df


COLUMNS_TO_EVALUATE: Final[list[str]] = [
    StoryPhraseColumns.REFERENCE_PHONEMES.value,
    StoryPhraseColumns.EXPECTED_TEXT.value,
]


def load_story_phrase_data(*, config: PipelineConfig) -> pl.DataFrame:
    """Load story phrase data from file and process string columns.

    Args:
        config: Configuration dictionary containing cached file paths.

    Returns:
        DataFrame with story phrase data and processed columns.

    Raises:
        FileNotFoundError: If story phrase file doesn't exist.
        ValueError: If file format is unsupported or required columns are missing.
    """
    story_phrase_path: Path = Path(config.cached["story_phrase_info"])
    parquet_path: Path = story_phrase_path.with_suffix(FileFormat.PARQUET)

    if not story_phrase_path.exists() and not parquet_path.exists():
        raise FileNotFoundError(f"Story phrase file not found: {story_phrase_path}")

    if parquet_path.exists():
        logger.info(f"Loading pre-processed story phrases from {parquet_path}")
        story_phrase_df: pl.DataFrame = pl.read_parquet(parquet_path)
    else:
        match story_phrase_path.suffix:
            case FileFormat.CSV:
                logger.info(
                    f"Loading and processing story phrases from {story_phrase_path}"
                )
                story_phrase_df: pl.DataFrame = pl.read_csv(story_phrase_path)

                required_columns: set[str] = {StoryPhraseColumns.EXPECTED_TEXT}
                missing_columns: set[str] = required_columns - set(
                    story_phrase_df.columns
                )
                if missing_columns:
                    raise ValueError(f"Missing required columns: {missing_columns}")

                for column in COLUMNS_TO_EVALUATE:
                    if column in story_phrase_df.columns:
                        story_phrase_df = story_phrase_df.with_column(
                            pl.col(column).map_elements(
                                literal_eval, return_dtype=pl.List
                            )
                        )

                logger.info(
                    f"Saving processed story phrases to {parquet_path} for future use"
                )
                story_phrase_df.write_parquet(parquet_path)
            case _:
                raise ValueError(
                    f"Unsupported file format: {story_phrase_path}. Use .csv or .parquet"
                )

    logger.info(f"Loaded {story_phrase_df.height} story phrases")
    return story_phrase_df


async def merge_activities_with_phrases(
    *, activity_df: pl.DataFrame, story_phrase_df: pl.DataFrame
) -> pl.DataFrame:
    """Create phrase-level data by merging activities with story phrases.

    Args:
        activity_df: DataFrame containing activity data
        story_phrase_df: DataFrame containing story phrase data

    Returns:
        DataFrame with merged activity and phrase data
    """
    phrase_df: pl.DataFrame = activity_df.join(
        story_phrase_df, on=MergeColumns.STORY_ID, how="left"
    )

    initial_count: int = phrase_df.height
    phrase_df = phrase_df.filter(pl.col(MergeColumns.PHRASE_INDEX).is_not_null())
    final_count: int = phrase_df.height

    if initial_count > final_count:
        logger.warning(
            f"Dropped {initial_count - final_count} rows without phrase data"
        )

    logger.info(
        f"Created phrase-level data: {phrase_df.height} phrases from {activity_df.height} activities"
    )

    return phrase_df
