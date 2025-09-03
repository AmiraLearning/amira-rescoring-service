"""Audio metadata extraction and processing utilities.

This module handles audio file metadata extraction and segment analysis,
extracted from phrase_slicing.py for better organization.
"""

import re
from typing import Any

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


def _parse_segment_filename(filename: str) -> SegmentHead:
    """Parse segment filename to extract metadata.

    Args:
        filename: Segment filename to parse

    Returns:
        SegmentHead: Parsed metadata
    """
    # Extract phrase index, sequence number from filename patterns
    # Examples: 000-000000-timestamp-v2.wav, 001-000001-timestamp-v2.wav

    try:
        # Pattern: phrase-sequence-timestamp-version.wav
        pattern = r"(\d{3})-(\d{6})-(\d+)-v\d\.wav"
        match = re.match(pattern, filename)

        if match:
            phrase_idx = int(match.group(1))
            sequence = match.group(2)

            return SegmentHead(
                segment_file=filename,
                phrase_index=phrase_idx,
                sequence_number=sequence,
                last_segment=None,  # Would need additional logic to determine
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
    max_phrase_index = -1

    for filename in segment_file_names:
        segment_meta = _parse_segment_filename(filename)

        if not segment_meta.success:
            logger.warning(f"Failed to parse segment filename: {filename}")
            continue

        phrase_index = segment_meta.phrase_index
        if phrase_index is None:
            continue

        max_phrase_index = max(max_phrase_index, phrase_index)

        # Group segments by phrase index
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

    # Calculate number of phrases
    num_phrases_in_act = max_phrase_index + 1 if max_phrase_index >= 0 else 0

    # Validate that all expected phrase indices are present
    missing_phrases = []
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
