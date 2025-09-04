"""Audio metadata extraction and processing utilities.

This module handles audio file metadata extraction and segment analysis,
extracted from phrase_slicing.py for better organization.
"""

import re
from typing import Any, Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from infra.s3_client import ProductionS3Client


class SegmentHead(BaseModel):
    """Segment metadata extracted from filename."""

    segment_file: str
    phrase_index: int | None
    sequence_number: str | None
    last_segment: str | None
    success: bool


class SegmentMetadataResult(BaseModel):
    """Result from segment metadata extraction."""

    phrase_metadata: dict[int, Any]
    num_phrases: int
    success: bool

    model_config = ConfigDict(arbitrary_types_allowed=True)


SEGMENT_FILENAME_PATTERN: Final[re.Pattern] = re.compile(r"(\d{3})-(\d{6})-(\d+)-v\d\.wav")


def _parse_segment_filename(filename: str) -> SegmentHead:
    """Parse segment filename to extract metadata.

    Extract phrase index, sequence number from filename patterns
    Examples: 000-000000-timestamp-v2.wav, 001-000001-timestamp-v2.wav

    Args:
        filename: Segment filename to parse

    Returns:
        SegmentHead: Parsed metadata
    """

    try:
        match: re.Match | None = SEGMENT_FILENAME_PATTERN.match(filename)

        if match:
            phrase_idx: int = int(match.group(1))
            sequence: str | None = match.group(2)

            return SegmentHead(
                segment_file=filename,
                phrase_index=phrase_idx,
                sequence_number=sequence,
                last_segment=None,
                success=True,
            )
    except Exception as e:
        logger.debug(f"Failed to parse filename {filename}: {e}")

    return SegmentHead(
        segment_file=filename,
        phrase_index=None,
        sequence_number=None,
        last_segment=None,
        success=False,
    )


async def get_segment_metadata(
    *,
    segment_file_names: list[str],
    s3_client: ProductionS3Client,
    activity_id: str | None = None,
    retry_limit: int | None = 2,
    stage_source: bool = False,
) -> SegmentMetadataResult:
    """Extract metadata from segment filenames and categorize by phrase structure.

    Args:
        segment_file_names: List of segment file names
        s3_client: S3 client instance
        activity_id: Activity ID for context
        retry_limit: Retry limit for S3 operations
        stage_source: Use staging bucket if True

    Returns:
        SegmentMetadataResult: Structured result with phrase metadata
    """
    if not segment_file_names:
        return SegmentMetadataResult(phrase_metadata={}, num_phrases=0, success=False)

    slices_by_phrase: dict[int, Any] = {}
    max_phrase_index: int = -1

    for filename in segment_file_names:
        segment_meta: SegmentHead = _parse_segment_filename(filename)

        if not segment_meta.success:
            logger.warning(f"Failed to parse segment filename: {filename}")
            continue

        phrase_index: int | None = segment_meta.phrase_index
        if phrase_index is None:
            continue

        max_phrase_index = max(max_phrase_index, phrase_index)

        if phrase_index not in slices_by_phrase:
            slices_by_phrase[phrase_index] = {
                "segments": [],
                "phrase_index": phrase_index,
            }

        slices_by_phrase[phrase_index]["segments"].append(
            {
                "filename": filename,
                "sequence": segment_meta.sequence_number,
                "metadata": segment_meta,
            }
        )

    num_phrases_in_act: int = max_phrase_index + 1 if max_phrase_index >= 0 else 0

    missing_phrases: list[int] = []
    for idx in range(num_phrases_in_act):
        if idx not in slices_by_phrase:
            missing_phrases.append(idx)

    if missing_phrases:
        logger.warning(f"Missing phrase indices: {missing_phrases}")
        return SegmentMetadataResult(
            phrase_metadata=slices_by_phrase,
            num_phrases=num_phrases_in_act,
            success=False,
        )

    return SegmentMetadataResult(
        phrase_metadata=slices_by_phrase, num_phrases=num_phrases_in_act, success=True
    )
