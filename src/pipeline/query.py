from ast import literal_eval
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

import polars as pl
from loguru import logger

from infra.athena_client import query_athena
from utils.config import PipelineConfig

ACTIVITY_QUERY: Final[str] = """
    SELECT activityid,
        storyid,
        studentid,
        districtid,
        type,
        createdat,
        status,
        displaystatus
    FROM activity_v
    WHERE type = 'letterNamingAndSounds'
    AND status = 'under_review'
    AND createdat >= TIMESTAMP '{start_time}'
    AND createdat < TIMESTAMP '{end_time}'
    AND districtid IN ('955168', '1000022968')
"""

COLUMN_MAPPING: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "activityid": "activityId",
        "storyid": "storyId",
        "studentid": "studentId",
        "districtid": "districtId",
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
    """Fetch activity data based on config and save activities.parquet.

    Args:
        config: Pipeline configuration containing metadata and result settings.

    Returns:
        DataFrame with activity data.

    Raises:
        ValueError: If no activities found for the specified date range.
    """
    result_dir: str = config.result.output_dir

    if config.metadata.activity_id:
        logger.info(f"Loading specific activity via GraphQL: {config.metadata.activity_id}")
        import asyncio

        from infra.appsync_utils import load_activity_from_graphql

        activity_data = await asyncio.to_thread(
            load_activity_from_graphql, activity_id=config.metadata.activity_id
        )
        activity_df: pl.DataFrame = pl.DataFrame(activity_data)

        activity_raw_path: Path = Path(result_dir) / "activities.parquet"
        activity_raw_path.parent.mkdir(parents=True, exist_ok=True)
        activity_df.write_parquet(activity_raw_path)
        logger.info(f"GraphQL activity data saved: {activity_raw_path}")

        return activity_df
    elif config.metadata.activity_file:
        logger.info(f"Loading existing activity data from {config.metadata.activity_file}")
        activity_file_path = Path(config.metadata.activity_file)
        if activity_file_path.suffix == ".parquet":
            activity_df = pl.read_parquet(config.metadata.activity_file)
        else:
            activity_df = pl.read_csv(config.metadata.activity_file)
    else:
        logger.info("Fetching activities from data lake")
        if not config.metadata.processing_start_time or not config.metadata.processing_end_time:
            raise ValueError("Processing time range must be set")
        query_start_time = config.metadata.processing_start_time.strftime("%Y-%m-%d %H:%M:%S")
        query_end_time = config.metadata.processing_end_time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Using time range: {query_start_time} to {query_end_time}")

        activity_df = await query_athena(
            query=ACTIVITY_QUERY.format(
                start_time=query_start_time,
                end_time=query_end_time,
            )
        )

        logger.info(f"Fetched {activity_df.height} activities")
        if activity_df.is_empty():
            raise ValueError("No activities found for the specified date range")

        activity_df = activity_df.rename(COLUMN_MAPPING)

        activity_raw_file_path: Path = Path(result_dir) / "activities.parquet"
        activity_raw_file_path.parent.mkdir(parents=True, exist_ok=True)
        activity_df.write_parquet(activity_raw_file_path)
        logger.info(f"Activity raw data saved: {activity_raw_file_path}")

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
    if not config.cached.story_phrase_path:
        raise FileNotFoundError("Story phrase file path is not configured")

    story_phrase_path: Path = Path(config.cached.story_phrase_path)

    if not story_phrase_path.exists():
        raise FileNotFoundError(f"Story phrase file not found: {story_phrase_path}")

    if story_phrase_path.suffix == FileFormat.PARQUET:
        logger.info(f"Loading pre-processed story phrases from {story_phrase_path}")
        story_phrase_df: pl.DataFrame = pl.read_parquet(story_phrase_path)
    elif story_phrase_path.suffix == FileFormat.CSV:
        logger.info(f"Loading and processing story phrases from {story_phrase_path}")
        story_phrase_df = pl.read_csv(story_phrase_path)
    else:
        raise ValueError(
            f"Unsupported file format: {story_phrase_path.suffix}. Use {FileFormat.PARQUET} or {FileFormat.CSV}"
        )

    REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset({StoryPhraseColumns.EXPECTED_TEXT})
    missing_columns: set[str] = set(REQUIRED_COLUMNS) - set(story_phrase_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    columns_to_process: list[pl.Expr] = []
    for column in COLUMNS_TO_EVALUATE:
        if column in story_phrase_df.columns:
            columns_to_process.append(
                pl.col(column).map_elements(literal_eval, return_dtype=pl.Object)
            )

    if columns_to_process:
        story_phrase_df = story_phrase_df.with_columns(columns_to_process)

    if story_phrase_path.suffix == FileFormat.PARQUET:
        logger.info(
            "Story phrases loaded in parquet format; skipping re-save as it is already optimized"
        )
    else:
        logger.info(
            "Processed story phrases contain Object columns; skip parquet save to avoid schema issues"
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

    logger.debug(f"Initial phrase count: {initial_count}")
    logger.debug(f"Phrase dataframe: {phrase_df.head()}")
    phrase_df = phrase_df.filter(pl.col(MergeColumns.PHRASE_INDEX).is_not_null())
    final_count: int = phrase_df.height

    if initial_count > final_count:
        logger.warning(f"Dropped {initial_count - final_count} rows without phrase data")

    logger.info(
        f"Created phrase-level data: {phrase_df.height} phrases from {activity_df.height} activities"
    )

    return phrase_df
