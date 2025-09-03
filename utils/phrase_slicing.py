"""Generate reconstituted tutor phrase slices.

Given a tutor activity id,
- pulls segments of audio from the environment specified
- reconstitutes phrases of audio and writes them to disk

Intended usage:
- invoke process_activity_into_phrase_sliced_audio
- this function returns "True" if the activity was successfully processed
"""

import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from loguru import logger
from pydantic import BaseModel

from infra.s3_client import ProductionS3Client, S3OperationResult
from utils.audio_metadata import SegmentMetadataResult

# Use centralized bucket resolver to avoid duplication
from utils.s3_audio_operations import bucket_for as _bucket_for

SEGMENTS_TXT_FILES_BY_ACT: Final[str] = "segments_txt_files_by_act"
RECONSTITUTED_PHRASE_AUDIO: Final[str] = "reconstituted_phrase_audio"
SEGMENTS_BY_ACT: Final[str] = "segments_by_act"
MANIFEST_FILENAME: Final[str] = "segments_manifest.json"


class S3Object(BaseModel):
    key: str
    last_modified: datetime | None = None


async def s3_find(
    *,
    bucket: str,
    prefix: str = "/",
    delimiter: str = "/",
    start_after: str = "",
    s3_client: ProductionS3Client,
    include_last_mod: bool = False,
) -> list[S3Object]:
    """Enumerate S3 prefix (as a virtual directory)

    Args:
        bucket: Bucket containing the objects
        prefix: Prefix to enumerate
        delimiter: Virtual directory separator
        start_after: Don't enumerate objects with lexical names before this start point
        s3_client: ProductionS3Client to use
        include_last_mod: include last modification timestamp in result

    Returns:
        List of S3Object instances
    """
    cleaned_prefix: str = prefix.lstrip(delimiter)
    result = await s3_client.list_objects_batch([(bucket, cleaned_prefix)])

    if not result or not result[0].success:
        logger.warning(f"Failed to list objects in s3://{bucket}/{prefix}")
        return []

    objects_data: list[dict[str, Any]] = result[0].data.get("objects", [])

    s3_objects: list[S3Object] = []
    for obj in objects_data:
        key: str = obj["Key"]

        if start_after and key <= start_after:
            continue

        if include_last_mod:
            s3_objects.append(S3Object(key=key, last_modified=obj.get("LastModified")))
        else:
            s3_objects.append(S3Object(key=key))

    return s3_objects


def bucket_for(*, stage_source: bool) -> str:
    """Delegate to unified bucket resolver."""
    return _bucket_for(stage_source=stage_source)


def _ensure_parent_dir(*, path: str) -> None:
    """Ensure parent directory exists for a target path.

    Args:
        path: The path to ensure the parent directory exists for.
    """
    parent: Path = Path(path).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


async def get_segment_file_names(
    *, activity_id: str, s3_client: ProductionS3Client, stage_source: bool = False
) -> list[str]:
    """Get the file names of all the audio segments for an activity.

    Args:
        activity_id: Activity identifier.
        s3_client: ProductionS3Client to use.
        stage_source: Whether to use the staging bucket.

    Returns:
        List of object keys corresponding to audio segments.
    """
    bucket: str = bucket_for(stage_source=stage_source)
    keys: list[str] = []
    objects = await s3_find(bucket=bucket, prefix=f"{activity_id}/", s3_client=s3_client)
    for obj in objects:
        key: str = obj.key
        if ".wav" in key and "complete" not in key:
            keys.append(key)
        else:
            logger.error(f"skipping non-phrase key under s3://{bucket}/{activity_id}: {key}")
    return keys


async def load_activity_manifest(
    *, activity_id: str, s3_client: ProductionS3Client, stage_source: bool = False
) -> dict[str, Any] | None:
    """Load segments manifest for an activity if present.

    Returns manifest dict or None if missing/unavailable.
    """
    bucket: str = bucket_for(stage_source=stage_source)
    manifest_key: str = f"{activity_id}/{MANIFEST_FILENAME}"
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp_path: str = tf.name
    try:
        results: list[S3OperationResult] = await s3_client.download_files_batch(
            [(bucket, manifest_key, tmp_path)]
        )
        if not results or not results[0].success:
            return None
        with open(tmp_path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load manifest for {activity_id}: {e}")
        return None
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


async def write_activity_manifest(
    *,
    activity_id: str,
    segments: list[str],
    slices_by_phrase: dict[int, Any],
    num_phrases_in_act: int,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> None:
    """Write segments manifest for an activity for future runs."""
    bucket: str = bucket_for(stage_source=stage_source)
    manifest_key: str = f"{activity_id}/{MANIFEST_FILENAME}"
    payload: dict[str, Any] = {
        "activity_id": activity_id,
        "segments": segments,
        "slices_by_phrase": slices_by_phrase,
        "num_phrases_in_act": num_phrases_in_act,
        "schema_version": 1,
    }
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp_path: str = tf.name
        tf.write(json.dumps(payload).encode("utf-8"))
    try:
        await s3_client.upload_files_batch([(tmp_path, bucket, manifest_key)])
    except Exception as e:
        logger.warning(f"Failed to write manifest for {activity_id}: {e}")
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


async def download_segment_files(
    *,
    segment_file_names: list[str],
    destination_path: str,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
) -> bool:
    """Download a list of files (in this case, lil' tiny audio segments)."""

    if len(segment_file_names) == 0:
        logger.info("no segment files")
        return False

    mkdir_path: Path = Path(destination_path) / segment_file_names[0]
    _ensure_parent_dir(path=str(mkdir_path))

    bucket: str = bucket_for(stage_source=stage_source)

    download_operations: list[tuple[str, str, str]] = [
        (bucket, segment_file, str(Path(destination_path) / segment_file))
        for segment_file in segment_file_names
    ]

    results: list[S3OperationResult] = await s3_client.download_files_batch(download_operations)

    successful_downloads: list[S3OperationResult] = [r for r in results if r.success]

    if len(successful_downloads) != len(segment_file_names):
        failed_downloads: list[S3OperationResult] = [r for r in results if not r.success]
        for failed in failed_downloads:
            logger.warning(f"Failed to download {failed.operation.key}: {failed.error}")
        return False

    return True


class SegmentHead(BaseModel):
    segment_file: str
    phrase_index: int | None
    sequence_number: str | None
    last_segment: str | None
    success: bool


async def get_segment_metadata(
    *,
    segment_file_names: list[str],
    s3_client: ProductionS3Client,
    activity_id: str | None = None,
    retry_limit: int | None = 2,
    stage_source: bool = False,
) -> SegmentMetadataResult:
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

    bucket = bucket_for(stage_source=stage_source)

    head_requests: list[tuple[str, str]] = [
        (bucket, segment_file) for segment_file in segment_file_names
    ]
    head_results: list[S3OperationResult] = await s3_client.head_objects_batch(head_requests)

    metadata_across_segments: list[SegmentHead] = []

    for idx, result in enumerate(head_results):
        segment_file: str = segment_file_names[idx]

        if result.success and result.data:
            metadata: dict[str, Any] = result.data.get("metadata", {})
            try:
                current_segment_head: SegmentHead = SegmentHead(
                    segment_file=segment_file,
                    phrase_index=int(metadata.get("Metadata", {}).get("phraseindex", -1)),
                    sequence_number=metadata.get("Metadata", {}).get("sequencenumber"),
                    last_segment=metadata.get("Metadata", {}).get("lastsegment"),
                    success=True,
                )
            except (ValueError, KeyError) as e:
                logger.warning(f"Failed to parse metadata for {segment_file}: {e}")
                current_segment_head = SegmentHead(
                    segment_file=segment_file,
                    phrase_index=None,
                    sequence_number=None,
                    last_segment=None,
                    success=False,
                )
        else:
            logger.warning(f"Failed to get metadata for {segment_file}: {result.error}")
            current_segment_head = SegmentHead(
                segment_file=segment_file,
                phrase_index=None,
                sequence_number=None,
                last_segment=None,
                success=False,
            )

        metadata_across_segments.append(current_segment_head)

    slices_by_phrase: dict[int, Any] = {}
    num_phrases_in_act: int = 0
    for segment_meta in metadata_across_segments:
        if not segment_meta.success:
            if activity_id is None:
                logger.info(
                    f"retrying due to failed custom segment metadata head call: segment_meta {segment_meta!s}..."
                )
            else:
                logger.info(
                    f"retrying due to failed custom segment metadata head call: activity_id {activity_id}..."
                )
            for idx in range(retry_limit or 2):
                segment_meta_file: str = segment_meta.segment_file
                logger.info(
                    f"retry {idx} during segment metadata head call: segment_meta_file {segment_meta_file}..."
                )

                retry_results: list[S3OperationResult] = await s3_client.head_objects_batch(
                    [(bucket, segment_meta_file)]
                )

                if retry_results and retry_results[0].success:
                    retry_result: S3OperationResult = retry_results[0]
                    retry_metadata: dict[str, Any] = retry_result.data.get("metadata", {})
                    try:
                        updated_segment_meta: SegmentHead = SegmentHead(
                            segment_file=segment_meta_file,
                            phrase_index=int(
                                retry_metadata.get("Metadata", {}).get("phraseindex", -1)
                            ),
                            sequence_number=retry_metadata.get("Metadata", {}).get(
                                "sequencenumber"
                            ),
                            last_segment=retry_metadata.get("Metadata", {}).get("lastsegment"),
                            success=True,
                        )
                        segment_meta = updated_segment_meta
                        break
                    except (ValueError, KeyError):
                        continue
            if not segment_meta.success:
                return SegmentMetadataResult(phrase_metadata={}, num_phrases=0, success=False)
        phrase_index: int | None = segment_meta.phrase_index
        sequence_number: str | None = segment_meta.sequence_number
        last_segment: str | None = segment_meta.last_segment

        if phrase_index is not None:
            if (phrase_index + 1) > num_phrases_in_act:
                num_phrases_in_act = phrase_index + 1

            if phrase_index not in slices_by_phrase.keys():
                slices_by_phrase[phrase_index] = {"last_segment": {}, "segments": {}}

            slices_by_phrase[phrase_index]["segments"][sequence_number] = (
                segment_meta.segment_file.split("/")[-1]
            )
            if last_segment:
                slices_by_phrase[phrase_index]["last_segment"] = {
                    "file_name": segment_meta.segment_file,
                    "sequence_number": sequence_number,
                }

    if not all(idx in slices_by_phrase for idx in range(num_phrases_in_act)):
        logger.info("not all phrases present in segments")
        return SegmentMetadataResult(
            phrase_metadata=slices_by_phrase,
            num_phrases=num_phrases_in_act,
            success=False,
        )

    return SegmentMetadataResult(
        phrase_metadata=slices_by_phrase, num_phrases=num_phrases_in_act, success=True
    )


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
        full_path: Path = (Path(root_dir) / segment_path).resolve()
        return f"file '{full_path}'"

    def _concat_export(*, concat_file: str, export_file: str) -> None:
        """Concatenate audio files using ffmpeg concat demuxer for performance.

        Args:
            concat_file: Path to the file containing the list of files to concatenate.
            export_file: Path to the output file.
        """
        import subprocess

        logger.info("Concatenating audio files with ffmpeg concat demuxer")
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-threads",
            "2",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(export_file),
            "-y",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(f"ffmpeg concat copy failed; falling back to re-encode: {result.stderr}")
            fallback_cmd: list[str] = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-threads",
                "2",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(export_file),
                "-y",
            ]
            result2 = subprocess.run(fallback_cmd, capture_output=True, text=True)
            if result2.returncode != 0:
                logger.error(f"ffmpeg concat re-encode failed: {result2.stderr}")
                raise RuntimeError("Phrase concatenation failed")

    def _phrase_start_index(*, phrase_index: int) -> int:
        """Get the start index for a phrase.

        Args:
            phrase_index: Phrase index.

        Returns:
            int: Start index.
        """
        if phrase_index == 0:
            return 0
        return int(slices_by_phrase[phrase_index - 1]["last_segment"]["sequence_number"]) + 1

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
                seg: str = phrase_meta["segments"][str(curr)]
                if phrase_dir is None:
                    phrase_dir = str(Path(phrase_segments_root_dir) / activity_id)
                # Reconstruct full segment path since we now store only basename
                segment_full_path = f"{activity_id}/{seg}"
                files.append(
                    _concat_input_line(
                        root_dir=phrase_segments_root_dir, segment_path=segment_full_path
                    )
                )
                dropped = 0
            except Exception as e:
                logger.info(f"Dropped packet, phrase {phrase_index}, activity {activity_id}")
                logger.info(e)
                dropped += 1
                if dropped > 5:
                    logger.info("CROSSED CONSECUTIVE DROPPED PACKETS THRESHOLD")
            curr += 1
        return files, phrase_dir

    def reconstitute_phrase(*, phrase_index: int) -> str | None:
        """Reconstitute a phrase.

        Args:
            phrase_index: Phrase index.

        Returns:
            bool: True if the phrase was reconstructed and saved successfully, False otherwise.
        """
        logger.info(f"loading segments for phrase {phrase_index}, {activity_id}...")
        files, phrase_dir = _build_concat_list(phrase_index=phrase_index)
        export_destination_file: Path = export_destination_folder / f"phrase_{phrase_index}.wav"
        phrase_segments_concat_file: Path = (
            phrase_segments_concat_dir / f"phrase_{phrase_index}.txt"
        )
        phrase_segments_concat_file.parent.mkdir(parents=True, exist_ok=True)
        with open(phrase_segments_concat_file, "w") as f:
            for line in files:
                f.write(f"{line}\n")
        _concat_export(
            concat_file=str(phrase_segments_concat_file),
            export_file=str(export_destination_file),
        )
        return phrase_dir

    phrase_indices: list[int] = list(range(num_phrases_in_act))
    phrase_segments_activity_dirs: list[str | None] = [None] * len(phrase_indices)

    max_workers: int = min(6, max(1, len(phrase_indices)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(reconstitute_phrase, phrase_index=idx): idx for idx in phrase_indices
        }
        for fut in as_completed(future_map):
            idx: int = future_map[fut]
            try:
                phrase_segments_activity_dirs[idx] = fut.result()
            except Exception as e:
                logger.error(f"Failed to reconstitute phrase {idx} for activity {activity_id}: {e}")
                phrase_segments_activity_dirs[idx] = None

    if not keep_intermediaries:
        try:
            shutil.rmtree(phrase_segments_concat_dir)
            for item in frozenset(phrase_segments_activity_dirs):
                if item is not None:
                    shutil.rmtree(item)
        except Exception as e:
            logger.warning(
                f"could not delete intermediaries for activity {activity_id} in reconstruction: {e!s}"
            )
            return False

    return all([item is not None for item in phrase_segments_activity_dirs])


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
        s3_client: ProductionS3Client | None = None,
        stage_source: bool = False,
    ):
        """Initialize the PhraseSlicer

        Args:
            destination_path: Path to the destination directory
            s3: S3 client
            stage_source: Whether to use the staging bucket
        """
        from infra.s3_client import HighPerformanceS3Config

        self.s3_client: ProductionS3Client = s3_client or ProductionS3Client(
            config=HighPerformanceS3Config()
        )
        self.destination_path: str = (
            destination_path if destination_path.endswith("/") else (destination_path + "/")
        )
        self.stage_source: bool = stage_source

    async def process_activity_into_phrase_sliced_audio(
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
        segment_file_names: list[str] = await get_segment_file_names(
            activity_id=source_activity_id,
            s3_client=self.s3_client,
            stage_source=self.stage_source,
        )

        logger.info(f"downloading / categorizing metadata {source_activity_id}...")
        metadata_result = await get_segment_metadata(
            segment_file_names=segment_file_names,
            s3_client=self.s3_client,
            activity_id=source_activity_id,
            retry_limit=2,
            stage_source=self.stage_source,
        )
        slices_by_phrase = metadata_result.phrase_metadata
        num_phrases_in_act = metadata_result.num_phrases
        success_bool = metadata_result.success

        logger.debug(f"slices_by_phrase: {slices_by_phrase}")
        logger.debug(f"num_phrases_in_act: {num_phrases_in_act}")
        logger.info(f"success_bool: {success_bool}")
        if not success_bool:
            return success_bool

        logger.info(f"downloading segments {source_activity_id}...")
        logger.debug(f"segment_file_names: {segment_file_names}")
        logger.debug(f"destination_path: {self.destination_path}")
        logger.debug(f"s3_client: {self.s3_client}")
        logger.debug(f"stage_source: {self.stage_source}")
        download_success_bool: bool = await download_segment_files(
            segment_file_names=segment_file_names,
            destination_path=self.destination_path.replace(
                RECONSTITUTED_PHRASE_AUDIO, SEGMENTS_BY_ACT
            ),
            s3_client=self.s3_client,
            stage_source=self.stage_source,
        )
        if not download_success_bool:
            return download_success_bool

        logger.info(f"reconstituting phrase audio {source_activity_id}...")
        final_target_activity_id: str = (
            source_activity_id[:-4] + replay_suffix
            if replay_suffix is not None
            else source_activity_id
        )
        reconstitute_success_bool: bool = reconstitute_and_save_phrase(
            num_phrases_in_act=num_phrases_in_act,
            slices_by_phrase=slices_by_phrase,
            destination_path=self.destination_path,
            activity_id=final_target_activity_id,
            keep_intermediaries=keep_intermediaries,
        )
        return reconstitute_success_bool
