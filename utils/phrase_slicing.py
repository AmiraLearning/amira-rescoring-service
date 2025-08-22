"""Generate reconstituted tutor phrase slices.

Given a tutor activity id,
- pulls segments of audio from the environment specified
- reconstitutes phrases of audio and writes them to disk

Intended usage:
- invoke process_activity_into_phrase_sliced_audio
- this function returns "True" if the activity was successfully processed
"""

from pathlib import Path
import subprocess
import time
import shutil
from typing import Any, Final
from pydantic import BaseModel

import boto3
import botocore
from loguru import logger

from utils.download_audio import s3_find

BotoClient = Any
BotoResource = Any
_S3_SPEECH_ROOT_PROD: Final[str] = "amira-speech-stream"
_S3_SPEECH_ROOT_STAGE: Final[str] = "amira-speech-stream-stage"
SEGMENTS_TXT_FILES_BY_ACT: Final[str] = "segments_txt_files_by_act"
RECONSTITUTED_PHRASE_AUDIO: Final[str] = "reconstituted_phrase_audio"
SEGMENTS_BY_ACT: Final[str] = "segments_by_act"


def bucket_for(*, stage_source: bool) -> str:
    """Resolve bucket name based on environment."""
    return _S3_SPEECH_ROOT_STAGE if stage_source else _S3_SPEECH_ROOT_PROD


def _ensure_parent_dir(*, path: str) -> None:
    """Ensure parent directory exists for a target path."""
    parent: Path = Path(path).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def get_segment_file_names(
    *, activity_id: str, s3_client: BotoClient, stage_source: bool = False
) -> list[str]:
    """Get the file names of all the audio segments for an activity.

    Args:
        activity_id: Activity identifier.
        s3: Boto3 S3 client.
        stage_source: Whether to use the staging bucket.

    Returns:
        List of object keys corresponding to audio segments.
    """
    bucket: str = bucket_for(stage_source=stage_source)
    keys: list[str] = []
    for obj in s3_find(bucket=bucket, prefix=f"{activity_id}/", s3_client=s3_client):
        key: str = obj if isinstance(obj, str) else getattr(obj, "key", "")
        if ".wav" in key and "complete" not in key:
            keys.append(key)
        else:
            logger.error(
                f"skipping non-phrase key under s3://{bucket}/{activity_id}: {key}"
            )
    return keys


def download_segment_files(
    *,
    segment_file_names: list[str],
    destination_path: str,
    s3_client: BotoClient,
    stage_source: bool = False,
) -> bool:
    """Download a list of files (in this case, lil' tiny audio segments)."""

    if len(segment_file_names) == 0:
        logger.info("no segment files")
        return False

    mkdir_path: Path = Path(destination_path) / segment_file_names[0]
    _ensure_parent_dir(path=mkdir_path)

    def download_segment(segment_file: str, attempt: int = 0) -> bool:
        dest_pathname: Path = Path(destination_path) / segment_file
        try:
            bucket: str = bucket_for(stage_source=stage_source)
            s3_client.download_file(bucket, segment_file, dest_pathname)
        except Exception as e:
            logger.info(e)
            logger.info(f"attempt #{str(attempt)}, {segment_file}, {dest_pathname}")
            logger.info(segment_file)
            logger.info(dest_pathname)
            if attempt < 5:
                time.sleep(0.1)
                return download_segment(segment_file, attempt=(attempt + 1))
            else:
                logger.warning(
                    f"could not download segment {segment_file} in {str(attempt)} attempts"
                )
                return False
        return True

    # Simple parallelization placeholder: sequential to preserve behavior
    return all(download_segment(sf) for sf in segment_file_names)


class SegmentHead(BaseModel):
    segment_file: str
    phrase_index: int | None
    sequence_number: str | None
    last_segment: str | None
    success: bool


def get_segment_metadata(
    *,
    segment_file_names: list[str],
    s3_client: BotoClient,
    activity_id: str | None = None,
    retry_limit: int | None = 2,
    stage_source: bool = False,
) -> tuple[dict[int, Any], int, bool]:
    # TODO let's nuke this tuple
    """For each slice filename, pull its metadata and categorize it by phrase structure.

    Args:
        segment_file_names: List of segment file names.
        s3_client: S3 client.
        activity_id: Activity ID.
        retry_limit: Retry limit.
        stage_source: Whether to use the staging bucket.

    Returns:
        Tuple containing:
        - Dictionary of phrase indices to their metadata
        - Number of phrases in the activity
        - Boolean indicating success
    """

    def make_segment_head_call(segment_file: str) -> SegmentHead:
        """Make a segment head call.

        Args:
            segment_file: Segment file name.

        Returns:
            SegmentHead: Segment head.
        """
        try:
            bucket = bucket_for(stage_source=stage_source)
            response = s3_client.head_object(Bucket=bucket, Key=segment_file)
            return SegmentHead(
                segment_file=segment_file,
                phrase_index=int(response["Metadata"]["phraseindex"]),
                sequence_number=response["Metadata"]["sequencenumber"],
                last_segment=response["Metadata"]["lastsegment"],
                success=True,
            )
        except Exception as e:
            logger.info(
                f"exception encountered while fetching segment metadata: {str(e)}"
            )
            return SegmentHead(
                segment_file=segment_file,
                phrase_index=None,
                sequence_number=None,
                last_segment=None,
                success=False,
            )

    # Preserve behavior: iterate in order; consider parallelization later
    metadata_across_segments: list[SegmentHead] = [
        make_segment_head_call(sf) for sf in segment_file_names
    ]

    slices_by_phrase: dict[int, Any] = {}
    num_phrases_in_act: int = 0
    for segment_meta in metadata_across_segments:
        if not segment_meta.success:
            if activity_id is None:
                logger.info(
                    f"retrying due to failed custom segment metadata head call: segment_meta {str(segment_meta)}..."
                )
            else:
                logger.info(
                    f"retrying due to failed custom segment metadata head call: activity_id {activity_id}..."
                )
            # Add a retry logic based on retry attempts
            for idx in range(retry_limit):
                segment_meta_file = segment_meta.segment_file
                logger.info(
                    f"retry {idx} during segment metadata head call: segment_meta_file {segment_meta_file}..."
                )
                segment_meta = make_segment_head_call(segment_meta_file)
                if segment_meta.success:
                    break
            if not segment_meta.success:
                return {}, 0, False
        segment_file = segment_meta.segment_file
        phrase_index = segment_meta.phrase_index
        sequence_number = segment_meta.sequence_number
        last_segment = segment_meta.last_segment

        # Track last phrase in act
        if (phrase_index + 1) > num_phrases_in_act:
            num_phrases_in_act = phrase_index + 1

        # Initialize data structure if necessary
        if phrase_index not in slices_by_phrase.keys():
            slices_by_phrase[phrase_index] = {"last_segment": {}, "segments": {}}

        # Organize phrase segment metadata
        slices_by_phrase[phrase_index]["segments"][sequence_number] = segment_file
        if last_segment:
            slices_by_phrase[phrase_index]["last_segment"] = {
                "file_name": segment_file,
                "sequence_number": sequence_number,
            }

    if not all(index in slices_by_phrase for index in range(num_phrases_in_act)):
        logger.info("not all phrases present in segments")
        return slices_by_phrase, num_phrases_in_act, False

    return slices_by_phrase, num_phrases_in_act, True


def reconstitute_and_save_phrase(
    *,
    num_phrases_in_act: int,
    slices_by_phrase: dict[int, Any],
    destination_path: str,
    activity_id: str,
    keep_intermediaries: bool,
) -> bool:
    """Reconstitute and save a phrase.

    Args:
        num_phrases_in_act: Number of phrases in the activity.
        slices_by_phrase: Dictionary of phrase indices to their metadata.
        destination_path: Path to the destination directory.
        activity_id: Activity ID.
        keep_intermediaries: Whether to keep intermediaries.

    Returns:
        bool: True if the phrase was reconstructed and saved successfully, False otherwise.
    """
    export_destination_folder: Path = Path(destination_path) / activity_id
    if export_destination_folder.exists():  # delete folder if it exists
        shutil.rmtree(export_destination_folder)
    export_destination_folder.mkdir(parents=True, exist_ok=True)

    phrase_segments_root_dir: str = destination_path.replace(
        RECONSTITUTED_PHRASE_AUDIO, SEGMENTS_BY_ACT
    )
    phrase_segments_concat_dir: Path = Path(
        destination_path.replace(RECONSTITUTED_PHRASE_AUDIO, SEGMENTS_TXT_FILES_BY_ACT),
        activity_id,
    )
    if not phrase_segments_concat_dir.parent.exists():
        phrase_segments_concat_dir.parent.mkdir(parents=True, exist_ok=True)

    def _concat_input_line(*, root_dir: str, segment_path: str) -> str:
        return f"file '{Path(root_dir) / segment_path}'"

    def _concat_export(*, concat_file: str, export_file: str) -> None:
        logger.info("Will now concatenate with ffmpeg:")
        subprocess.call(
            f"ffmpeg -nostdin -f concat -safe 0 -i {concat_file} -c copy {export_file} -y",
            shell=True,
        )

    def _phrase_start_index(*, phrase_index: int) -> int:
        """Get the start index for a phrase.

        Args:
            phrase_index: Phrase index.

        Returns:
            int: Start index.
        """
        if phrase_index == 0:
            return 0
        return (
            int(slices_by_phrase[phrase_index - 1]["last_segment"]["sequence_number"])
            + 1
        )

    def _build_concat_list(*, phrase_index: int) -> tuple[list[str], str | None]:
        """Build a list of concatenated files for a phrase.

        Args:
            phrase_index: Phrase index.

        Returns:
            tuple: List of concatenated files and the phrase directory.
        """
        phrase_meta: dict[str, Any] = slices_by_phrase[phrase_index]
        phrase_dir: str | None = None
        files: list[str] = []
        dropped: int = 0
        curr: int = _phrase_start_index(phrase_index=phrase_index)
        last_seq: int = int(phrase_meta["last_segment"]["sequence_number"])
        while curr <= last_seq:
            try:
                seg = phrase_meta["segments"][str(curr)]
                if phrase_dir is None:
                    phrase_dir = Path(phrase_segments_root_dir) / seg.split("/")[0]
                files.append(
                    _concat_input_line(
                        root_dir=phrase_segments_root_dir, segment_path=seg
                    )
                )
                dropped = 0
            except Exception as e:
                logger.info(
                    f"Dropped packet, phrase {phrase_index}, activity {activity_id}"
                )
                logger.info(e)
                dropped += 1
                if dropped > 5:
                    logger.info("CROSSED CONSECUTIVE DROPPED PACKETS THRESHOLD")
            curr += 1
        return files, phrase_dir

    def reconstitute_phrase(phrase_index: int):
        """Reconstitute a phrase.

        Args:
            phrase_index: Phrase index.

        Returns:
            bool: True if the phrase was reconstructed and saved successfully, False otherwise.
        """
        logger.info(f"loading segments for phrase {phrase_index}, {activity_id}...")
        files, phrase_dir = _build_concat_list(phrase_index=phrase_index)
        export_destination_file: Path = (
            export_destination_folder / f"phrase_{phrase_index}.wav"
        )
        phrase_segments_concat_file: Path = (
            phrase_segments_concat_dir / f"phrase_{phrase_index}.txt"
        )
        phrase_segments_concat_file.parent.mkdir(parents=True, exist_ok=True)
        with open(phrase_segments_concat_file, "w") as f:
            for line in files:
                f.write(f"{line}\n")
        _concat_export(
            concat_file=phrase_segments_concat_file, export_file=export_destination_file
        )
        return phrase_dir

    phrase_indices: list[int] = list(range(num_phrases_in_act))
    # Sequential to preserve ordering and side effects
    phrase_segments_activity_dirs: list[str | None] = [
        reconstitute_phrase(i) for i in phrase_indices
    ]

    if not keep_intermediaries:
        try:
            shutil.rmtree(phrase_segments_concat_dir)
            for d in frozenset(phrase_segments_activity_dirs):
                shutil.rmtree(d)
        except Exception as e:
            logger.warning(
                f"could not delete intermediaries for activity {activity_id} in reconstruction: {str(e)}"
            )
            return False

    return all([d is not None for d in phrase_segments_activity_dirs])


class PhraseSlicer:
    """Phrase slicer.

    Attributes:
        s3_client: S3 client.
        destination_path: Path to the destination directory.
        stage_source: Whether to use the staging bucket.
    """

    def __init__(
        self,
        *,
        destination_path: str,
        s3_client: BotoClient | None = None,
        stage_source: bool = False,
    ):
        """Initialize the PhraseSlicer

        Args:
            destination_path: Path to the destination directory
            s3: S3 client
            stage_source: Whether to use the staging bucket
        """
        self.s3_client: BotoClient = s3_client or boto3.client(
            "s3", config=botocore.client.Config(max_pool_connections=50)
        )
        self.destination_path: str = (
            destination_path
            if destination_path.endswith("/")
            else (destination_path + "/")
        )
        self.stage_source: bool = stage_source

    def process_activity_into_phrase_sliced_audio(
        self,
        *,
        activity_id: str,
        replay_suffix: str | None = None,
        keep_intermediaries: bool = False,
    ) -> bool:
        """Given a tutor id and destination path, reconstitutes phrase-sliced audio

        Args:
            activity_id: Activity ID.
            replay_suffix: Replay suffix.
            keep_intermediaries: Whether to keep intermediaries.

        Returns:
            bool: True if the activity was processed successfully, False otherwise.
        """
        source_activity_id: str = activity_id
        logger.info(f"getting file names for activity {source_activity_id}...")
        segment_file_names: list[str] = get_segment_file_names(
            activity_id=source_activity_id,
            s3_client=self.s3_client,
            stage_source=self.stage_source,
        )

        logger.info(f"downloading / categorizing metadata {source_activity_id}...")
        slices_by_phrase, num_phrases_in_act, success_bool = get_segment_metadata(
            segment_file_names=segment_file_names,
            s3_client=self.s3_client,
            activity_id=source_activity_id,
            retry_limit=2,
            stage_source=self.stage_source,
        )
        if not success_bool:
            return success_bool

        logger.info(f"downloading segments {source_activity_id}...")
        success_bool: bool = download_segment_files(
            segment_file_names=segment_file_names,
            destination_path=self.destination_path.replace(
                RECONSTITUTED_PHRASE_AUDIO, SEGMENTS_BY_ACT
            ),
            s3_client=self.s3_client,
            stage_source=self.stage_source,
        )
        if not success_bool:
            return success_bool

        logger.info(f"reconstituting phrase audio {source_activity_id}...")
        if replay_suffix is not None:
            target_activity_id = source_activity_id[:-4] + replay_suffix
        else:
            target_activity_id = source_activity_id
        success_bool: bool = reconstitute_and_save_phrase(
            num_phrases_in_act=num_phrases_in_act,
            slices_by_phrase=slices_by_phrase,
            destination_path=self.destination_path,
            activity_id=target_activity_id,
            keep_intermediaries=keep_intermediaries,
        )
        return success_bool
