"""Audio processing functionality for ORF rescoring pipeline.

This module contains functions for loading audio data from S3 and slicing audio
into phrase segments for processing. All functions use tempfiles and load audio
data into memory for efficient processing.
"""

import tempfile
from io import BytesIO
from typing import Final

from pydub import AudioSegment
from tenacity import retry, stop_after_attempt, wait_exponential

import amira_pyutils.s3 as s3_utils
from amira_pyutils.logging import get_logger
from src.orf_rescoring_pipeline.models import Activity

AUDIO_FILE_SUFFIX: Final[str] = ".wav"
AUDIO_FORMAT: Final[str] = "wav"
COMPLETE_AUDIO_FILENAME: Final[str] = "complete.wav"
RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_MIN_WAIT: Final[float] = 1.0
RETRY_MAX_WAIT: Final[float] = 10.0

logger = get_logger(__name__)


@retry(
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
    reraise=True,
)
async def _download_audio_from_s3(*, bucket: str, key: str, temp_file_path: str) -> None:
    """Download audio file from S3 to temporary file with retry logic.

    Args:
        bucket: S3 bucket name.
        key: S3 object key.
        temp_file_path: Path to temporary file for download.

    Raises:
        Exception: If S3 download fails after all retries.
    """
    s3_service = s3_utils.S3Service()
    await s3_service.download_file(bucket=bucket, key=key, filename=temp_file_path)


def _read_audio_file_to_bytes(*, file_path: str) -> bytes:
    """Read audio file from disk into bytes.

    Args:
        file_path: Path to audio file.

    Returns:
        Audio file data as bytes.

    Raises:
        IOError: If file cannot be read.
    """
    with open(file_path, "rb") as f:
        return f.read()


def _build_s3_audio_uri(*, activity_id: str, audio_bucket: str) -> str:
    """Build S3 URI for activity audio file.

    Args:
        activity_id: Unique activity identifier.
        audio_bucket: S3 bucket containing audio files.

    Returns:
        Complete S3 URI for the audio file.
    """
    return f"s3://{audio_bucket}/{activity_id}/{COMPLETE_AUDIO_FILENAME}"


def _get_cached_audio_segment(*, activity: Activity) -> AudioSegment:
    """Get cached AudioSegment or create and cache new one.

    Args:
        activity: Activity containing audio data.

    Returns:
        AudioSegment instance for the activity.

    Raises:
        ValueError: If activity has no audio file data.
    """
    if activity.audio_file_data is None:
        raise ValueError(f"No audio file data available for activity {activity.activity_id}")

    if hasattr(activity, "_audio_segment") and getattr(activity, "_audio_segment") is not None:
        return getattr(activity, "_audio_segment")

    audio = AudioSegment.from_file(BytesIO(activity.audio_file_data), format=AUDIO_FORMAT)
    setattr(activity, "_audio_segment", audio)
    return audio


def _find_phrase_timing(*, activity: Activity, phrase_index: int) -> tuple[int | None, int | None]:
    """Find start and end timing for a specific phrase.

    Args:
        activity: Activity containing page and phrase data.
        phrase_index: Index of the phrase to find timing for.

    Returns:
        Tuple of (start_ms, end_ms) or (None, None) if not found.
    """
    for page in activity.pages:
        try:
            rel_idx = page.phrase_indices.index(phrase_index)
            if page.aligned_phrases is not None:
                start_ms = page.aligned_phrases[rel_idx]["start"]
                end_ms = page.aligned_phrases[rel_idx]["end"]
            else:
                continue
            return start_ms, end_ms
        except (ValueError, IndexError):
            continue

    logger.warning(
        f"No aligned phrase found for activity {activity.activity_id}, phrase {phrase_index}"
    )
    return None, None


def _extract_phrase_audio(*, audio: AudioSegment, start_ms: int, end_ms: int) -> bytes:
    """Extract phrase audio segment and convert to bytes.

    Args:
        audio: Complete audio segment.
        start_ms: Start time in milliseconds.
        end_ms: End time in milliseconds.

    Returns:
        WAV audio data as bytes.
    """
    phrase_audio = audio[start_ms:end_ms]
    buf = BytesIO()
    phrase_audio.export(buf, format=AUDIO_FORMAT)
    return buf.getvalue()


async def load_activity_audio_data_from_s3(*, activity: Activity, audio_bucket: str) -> None:
    """Load audio data from S3 using temporary file to avoid permanent disk storage.

    Args:
        activity: Activity object to load audio data into.
        audio_bucket: S3 bucket containing the audio files.

    Raises:
        Exception: If audio loading fails after retries.
    """
    s3_uri = _build_s3_audio_uri(activity_id=activity.activity_id, audio_bucket=audio_bucket)
    s3_path = s3_utils.S3Address.from_uri(uri=s3_uri)

    with tempfile.NamedTemporaryFile(suffix=AUDIO_FILE_SUFFIX, delete=True) as temp_file:
        try:
            await _download_audio_from_s3(
                bucket=s3_path.bucket, key=s3_path.key, temp_file_path=temp_file.name
            )

            activity.audio_file_data = _read_audio_file_to_bytes(file_path=temp_file.name)

            logger.debug(f"Loaded audio data for activity {activity.activity_id} from S3")

        except Exception as e:
            logger.error(f"Failed to load audio data for activity {activity.activity_id}: {e}")
            activity.audio_file_data = None
            raise e


def slice_audio_file_data(
    *, activity: Activity, phrase_index: int
) -> tuple[bytes | None, tuple[int | None, int | None]]:
    """Slice audio file data to extract specific phrase segment.

    Args:
        activity: Activity containing audio data and page information.
        phrase_index: Index of the phrase to slice.

    Returns:
        Tuple containing:
            - audio_data: WAV audio data as bytes, or None if not found.
            - timing: Tuple of (start_ms, end_ms) for the phrase.
    """
    if activity.audio_file_data is None:
        logger.warning(f"No audio file data available for activity {activity.activity_id}")
        return None, (None, None)

    try:
        audio = _get_cached_audio_segment(activity=activity)
        start_ms, end_ms = _find_phrase_timing(activity=activity, phrase_index=phrase_index)

        if start_ms is None or end_ms is None:
            return None, (start_ms, end_ms)

        phrase_audio_data = _extract_phrase_audio(audio=audio, start_ms=start_ms, end_ms=end_ms)

        return phrase_audio_data, (start_ms, end_ms)

    except Exception as e:
        logger.error(
            f"Failed to slice audio for activity {activity.activity_id}, phrase {phrase_index}: {e}"
        )
        return None, (None, None)
