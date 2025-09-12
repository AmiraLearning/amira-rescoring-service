"""Phrase manifest generation for audio segment processing.

This module handles the construction of phrase manifests from individual audio segments
stored in S3. It provides functionality to fetch segment metadata, group segments by
phrase, and generate ordered phrase manifests with timing information.

Note: For assessment activities, the term "phrase" refers to the "page" in the manifest.
Copied from https://github.com/AmiraLearning/amira-slicing-service/blob/develop/amira_slicing/phrase_manifest.py
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Final

from tenacity import retry, stop_after_attempt, wait_exponential

# Audio processing constants
RIFF_HEADER_SIZE: Final[int] = 44
MAGIC_NUMBER_AUDIO_SIZE_TO_MS: Final[int] = 32
MAX_ALLOWED_INVALID_SEGMENTS: Final[int] = int(os.environ.get("MAX_ALLOWED_INVALID_SEGMENTS", "0"))

# S3 operation constants
DEFAULT_MAX_KEYS: Final[int] = 100
MAX_CONCURRENT_METADATA_WORKERS: Final[int] = 10

# Retry configuration
RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_MIN_WAIT: Final[float] = 1.0
RETRY_MAX_WAIT: Final[float] = 10.0

# Regex patterns
V2_SEGMENT_PATTERN: Final[str] = r"-v2.wav$"
V2_SEGMENT_EXTRACT_PATTERN: Final[str] = r".*?/(\d+)-(\d+)"
COMPLETE_WAV_SUFFIX: Final[str] = "complete.wav"


class InvalidV2Segment(Exception):
    """Raised when a V2 segment doesn't follow the expected naming convention."""


@dataclass
class Segment:
    """Represents an individual audio segment with metadata.

    Attributes:
        filename: The S3 key/filename of the segment.
        size: Size of the segment file in bytes.
        sequenceNumber: Optional sequence number within a phrase.
        phraseIndex: Optional index of the phrase this segment belongs to.
    """

    filename: str
    size: int
    sequenceNumber: int | None = None
    phraseIndex: int | None = None


@dataclass
class Phrase:
    """Represents a phrase composed of multiple audio segments.

    Attributes:
        phrase: The phrase index/identifier.
        duration: Total duration of the phrase in milliseconds.
        start: Start time of the phrase in milliseconds.
        end: End time of the phrase in milliseconds.
        segments: List of segments that make up this phrase.
    """

    phrase: int
    duration: int = 0
    start: int = 0
    end: int = 0
    segments: list[Segment] = field(default_factory=list)


@dataclass
class S3SegmentMetadata:
    """Metadata for an S3 audio segment.

    Attributes:
        sequenceNumber: Sequence number within a phrase.
        phraseIndex: Index of the phrase this segment belongs to.
    """

    sequenceNumber: int | None = None
    phraseIndex: int | None = None

    @staticmethod
    def from_dict(*, data: dict[str, str]) -> S3SegmentMetadata:
        """Create S3SegmentMetadata from a dictionary.

        Args:
            data: Dictionary containing metadata fields.

        Returns:
            S3SegmentMetadata instance with parsed values.
        """
        fields = ("sequenceNumber", "phraseIndex")
        vals = {k: data.get(str(k).lower()) for k in fields}

        converted_vals: dict[str, int | None] = {}
        for val in fields:
            raw_value = vals.get(val)
            if raw_value is not None:
                converted_vals[val] = int(raw_value)
            else:
                converted_vals[val] = None

        return S3SegmentMetadata(**converted_vals)


class PhraseBuilder:
    """Builds phrases from individual audio segments stored in S3."""

    def __init__(self, *, s3_client: Any) -> None:
        """Initialize the PhraseBuilder.

        Args:
            s3_client: Boto3 S3 client for accessing segment data.
        """
        self._s3_client = s3_client

    def build(self, *, bucket: str, activity_id: str) -> list[Segment]:
        """Build a list of segments with metadata for the given activity.

        Args:
            bucket: S3 bucket containing the segments.
            activity_id: Activity identifier used as S3 prefix.

        Returns:
            List of segments with populated metadata.

        Raises:
            InvalidV2Segment: If too many invalid segments are encountered.
        """
        segments = self._fetch_segments(bucket=bucket, activity_id=activity_id)
        segments_with_metadata = self._populate_segment_metadata(bucket=bucket, segments=segments)
        return segments_with_metadata

    def _populate_segment_metadata(self, *, bucket: str, segments: list[Segment]) -> list[Segment]:
        """Populate metadata for all segments using concurrent processing.

        Args:
            bucket: S3 bucket containing the segments.
            segments: List of segments to populate metadata for.

        Returns:
            List of segments with valid metadata, invalid segments removed.

        Raises:
            InvalidV2Segment: If too many invalid segments are encountered.
        """
        invalids: list[InvalidV2Segment] = []
        invalids_lock = threading.Lock()
        mutable_segments: list[Segment | None] = list(segments)

        def _fetch_single_segment_metadata(*, index: int, segment: Segment) -> tuple[int, bool]:
            """Fetch metadata for a single segment.

            Args:
                index: Index of the segment in the original list.
                segment: Segment to fetch metadata for.

            Returns:
                Tuple of (index, is_valid) indicating success.
            """
            try:
                meta = self._fetch_meta(bucket=bucket, key=segment.filename)
                self._apply_metadata_to_segment(segment=segment, metadata=meta)
                return index, True
            except InvalidV2Segment as error:
                with invalids_lock:
                    invalids.append(error)
                return index, False

        max_workers = min(len(segments), MAX_CONCURRENT_METADATA_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_single_segment_metadata, index=i, segment=segment): i
                for i, segment in enumerate(segments)
            }

            for future in as_completed(futures):
                i, is_valid = future.result()
                if not is_valid:
                    mutable_segments[i] = None

        if len(invalids) > MAX_ALLOWED_INVALID_SEGMENTS:
            raise invalids[0]

        return [s for s in mutable_segments if s is not None]

    def _apply_metadata_to_segment(self, *, segment: Segment, metadata: S3SegmentMetadata) -> None:
        """Apply metadata values to a segment.

        Args:
            segment: Segment to update.
            metadata: Metadata to apply.
        """
        for key, val in (
            ("sequenceNumber", metadata.sequenceNumber),
            ("phraseIndex", metadata.phraseIndex),
        ):
            if val is not None:
                setattr(segment, key, int(val))

    def _parse_segment_v2(self, *, filename: str) -> S3SegmentMetadata | None:
        """Parse V2 segment metadata from filename.

        Args:
            filename: S3 key/filename to parse.

        Returns:
            S3SegmentMetadata if V2 format, None otherwise.

        Raises:
            InvalidV2Segment: If filename has V2 tag but wrong format.
        """
        if not re.search(V2_SEGMENT_PATTERN, filename):
            return None

        parts = re.match(V2_SEGMENT_EXTRACT_PATTERN, filename)
        if parts is None:
            raise InvalidV2Segment(
                f"Segment name [{filename}] has V2 tag, but does not follow V2 convention"
            )

        phrase_index, seq_num = map(int, parts.groups())
        return S3SegmentMetadata(sequenceNumber=seq_num, phraseIndex=phrase_index)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
        reraise=True,
    )
    def _fetch_meta(self, *, bucket: str, key: str) -> S3SegmentMetadata:
        """Fetch metadata for a segment with retry logic.

        Args:
            bucket: S3 bucket name.
            key: S3 key for the segment.

        Returns:
            S3SegmentMetadata for the segment.
        """
        if meta := self._parse_segment_v2(filename=key):
            return meta

        head = self._s3_client.head_object(Bucket=bucket, Key=key)
        return S3SegmentMetadata.from_dict(data=head["Metadata"])

    def _fetch_segments(
        self, *, bucket: str, activity_id: str, max_keys: int = DEFAULT_MAX_KEYS
    ) -> list[Segment]:
        """Fetch all segments for an activity from S3.

        Args:
            bucket: S3 bucket name.
            activity_id: Activity identifier used as prefix.
            max_keys: Maximum keys to fetch per S3 request.

        Returns:
            List of segments found in S3.
        """
        segments: list[Segment] = []
        done = False
        next_token = None
        start_after = None

        while not done:
            kwargs = {
                "Bucket": bucket,
                "MaxKeys": max_keys,
                "Prefix": activity_id,
            }

            if next_token:
                kwargs["ContinuationToken"] = next_token

            if start_after:
                kwargs["StartAfter"] = start_after

            objs = self._s3_client.list_objects_v2(**kwargs)
            if objs["KeyCount"] == 0:
                return []

            done = not objs["IsTruncated"]
            for row in objs["Contents"]:
                filename = row["Key"]
                size = row["Size"]
                if filename.endswith(COMPLETE_WAV_SUFFIX):
                    continue
                segments.append(Segment(filename=filename, size=size))

            next_token = objs.get("NextContinuationToken")
            start_after = objs.get("StartAfter")

        return segments


class PhraseManifest:
    """Generates ordered phrase manifests from audio segments."""

    def __init__(self, *, builder: PhraseBuilder) -> None:
        """Initialize the PhraseManifest.

        Args:
            builder: PhraseBuilder instance for fetching segments.
        """
        self._builder = builder

    def generate(self, *, bucket: str, activity_id: str) -> list[Phrase]:
        """Generate an ordered phrase manifest for an activity.

        Args:
            bucket: S3 bucket containing the segments.
            activity_id: Activity identifier.

        Returns:
            Ordered list of phrases with timing information.
        """
        segments = self._builder.build(bucket=bucket, activity_id=activity_id)
        manifest = self._group_segments(segments=segments)
        return self._create_ordered_phrases(manifest=manifest)

    def _create_ordered_phrases(self, *, manifest: dict[int, Phrase]) -> list[Phrase]:
        """Create ordered list of phrases with timing information.

        Args:
            manifest: Dictionary mapping phrase indices to phrases.

        Returns:
            Ordered list of phrases with calculated timing.
        """
        ordered: list[Phrase] = []
        total_duration = 0

        for phrase_index in sorted(manifest):
            phrase = manifest[phrase_index]
            phrase.duration = round(phrase.duration / MAGIC_NUMBER_AUDIO_SIZE_TO_MS)
            phrase.start = total_duration
            phrase.end = phrase.duration + total_duration
            total_duration += phrase.duration
            ordered.append(phrase)

        return ordered

    @staticmethod
    def _group_segments(*, segments: list[Segment]) -> dict[int, Phrase]:
        """Group segments by phrase index.

        Args:
            segments: List of segments to group.

        Returns:
            Dictionary mapping phrase indices to Phrase objects.
        """
        manifest: dict[int, Phrase] = {}

        for segment in segments:
            phrase_index = segment.phraseIndex
            if phrase_index is None:
                continue
            phrase = manifest.get(phrase_index)
            if phrase is None:
                phrase = Phrase(phrase=phrase_index)

            phrase.segments.append(segment)
            phrase.duration += segment.size - RIFF_HEADER_SIZE
            manifest[phrase_index] = phrase

        return manifest
