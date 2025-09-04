import os
import threading
import warnings
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import numpy as np
import torch
import torchaudio
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from infra.s3_client import ProductionS3Client, S3OperationResult
from src.pipeline.exceptions import AudioProcessingError
from utils.config import S3_SPEECH_ROOT_PROD
from utils.extract_phrases import extract_phrase_slices_tutor_style

warnings.filterwarnings("ignore", category=UserWarning, module=r"torchaudio(\..*)?")

try:
    import torchaudio.load_with_torchcodec

    _USE_TORCHCODEC = True
except ImportError:
    _USE_TORCHCODEC = False

DEFAULT_SAMPLING_RATE: Final[int] = 16_000
DEFAULT_PADDED_SECONDS: Final[int] = 3
RECONSTITUTED_AUDIO_SUBDIR: Final[str] = "reconstituted_phrase_audio"
DEFAULT_AUDIO_SUBDIR: Final[str] = "DEFAULT"
PADDED_AUDIO_SUFFIX: Final[str] = "_padded.wav"
AUDIO_FILE_EXTENSION: Final[str] = ".wav"
AUDIO_BITS_PER_SAMPLE: Final[int] = 16
DEFAULT_AUDIO_READ_TIMEOUT_SEC: Final[int] = 10
AUDIO_MMAP_THRESHOLD_BYTES: Final[int] = 64 * 1024 * 1024


async def download_complete_audio_from_s3(
    *, activity_id: str, audio_dir: str, s3_client: ProductionS3Client
) -> bool:
    """Download complete.wav directly from S3 bucket.

    Args:
        activity_id: The unique identifier for the activity.
        audio_dir: The directory where audio files will be downloaded.
        s3_client: The S3 client.

    Returns:
        True if the complete audio was successfully downloaded, False otherwise.
    """
    try:
        complete_audio_dir: Path = Path(audio_dir) / "complete_audio" / activity_id
        complete_audio_dir.mkdir(parents=True, exist_ok=True)
        complete_audio_path: Path = complete_audio_dir / "complete.wav"

        if complete_audio_path.exists():
            logger.info(f"Complete audio already exists for {activity_id}")
            return True

        bucket: str = S3_SPEECH_ROOT_PROD
        s3_key: str = f"{activity_id}/complete.wav"

        result: list[S3OperationResult] = await s3_client.download_files_batch(
            [(bucket, s3_key, str(complete_audio_path))]
        )

        if result and result[0].success:
            logger.info(f"Successfully downloaded complete.wav for {activity_id}")
            return True
        else:
            logger.warning(f"Failed to download complete.wav for {activity_id}")
            return False

    except Exception as e:
        logger.error(f"Error downloading complete.wav for {activity_id}: {e}")
        return False


def _load_audio_file(*, file_path: Path) -> tuple[torch.Tensor, int]:
    """Load audio file using the appropriate torchaudio function.

    Args:
        file_path: Path to the audio file to load.

    Returns:
        Tuple of (audio_tensor, sample_rate)
    """
    import signal
    from concurrent.futures import ThreadPoolExecutor

    def timeout_handler(signum: int, frame: Any) -> None:
        raise TimeoutError("Audio file loading timed out")

    def _do_read() -> tuple[torch.Tensor, int]:
        size_bytes = file_path.stat().st_size if file_path.exists() else 0
        if size_bytes >= AUDIO_MMAP_THRESHOLD_BYTES:
            try:
                from scipy.io import wavfile

                sr, data = wavfile.read(str(file_path), mmap=True)
                tensor = torch.from_numpy(data.astype(np.float32))
                if tensor.ndim == 1:
                    tensor = tensor.unsqueeze(0)
                return tensor, int(sr)
            except (ImportError, RuntimeError, OSError) as e:
                logger.debug(f"torchcodec loading failed: {type(e).__name__}: {e}")
        use_torchcodec_env = os.getenv("AUDIO_USE_TORCHCODEC", "auto").lower()
        use_torchcodec: bool = (
            _USE_TORCHCODEC
            if use_torchcodec_env == "auto"
            else use_torchcodec_env in {"1", "true", "yes", "on"}
        )
        if use_torchcodec and _USE_TORCHCODEC:
            return torchaudio.load_with_torchcodec(str(file_path))
        import warnings

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="torchaudio")
            return torchaudio.load(str(file_path))

    use_signal: bool = threading.current_thread() is threading.main_thread()
    if use_signal:
        signal.signal(signal.SIGALRM, timeout_handler)
        timeout_val = int(os.getenv("AUDIO_READ_TIMEOUT_SEC", str(DEFAULT_AUDIO_READ_TIMEOUT_SEC)))
        signal.alarm(timeout_val)
        try:
            return _do_read()
        finally:
            signal.alarm(0)
    else:
        timeout_val = int(os.getenv("AUDIO_READ_TIMEOUT_SEC", str(DEFAULT_AUDIO_READ_TIMEOUT_SEC)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_do_read)
            return future.result(timeout=timeout_val)


# Fast WAV header preflight to avoid loading empty files
def _is_wav_empty(*, path: Path) -> bool:
    """Fast check for empty WAV without decoding.

    Tries standard WAV header via wave module; if that fails, uses file size
    heuristic; finally falls back to torchaudio.info when needed.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() == 0
    except (OSError, wave.Error) as e:
        logger.debug(f"WAV open failed: {type(e).__name__}: {e}")
        try:
            # 44 bytes is common PCM WAV header size; header-only => empty
            return path.exists() and path.stat().st_size <= 44
        except (OSError, AttributeError) as e:
            logger.debug(f"File stat check failed: {type(e).__name__}: {e}")
        try:
            info = torchaudio.info(str(path))
            return int(getattr(info, "num_frames", 0)) == 0
        except (RuntimeError, OSError, AttributeError) as e:
            logger.warning(
                f"Failed to determine WAV frame count for {path}: {type(e).__name__}: {e}"
            )
            return True


@lru_cache(maxsize=8)
def _get_resampler(*, orig: int) -> torchaudio.transforms.Resample:
    return torchaudio.transforms.Resample(orig_freq=orig, new_freq=DEFAULT_SAMPLING_RATE)


@lru_cache(maxsize=64)
def _load_and_resample(*, path: Path) -> torch.Tensor | None:
    if not path.exists() or _is_wav_empty(path=path):
        return None
    speech, sr = _load_audio_file(file_path=path)
    resampler = _get_resampler(orig=sr)
    return resampler(speech.squeeze(0))


# TODO optimize this
class AudioSegmentRequest(BaseModel):
    """Request model for audio segment loading."""

    file_path: Path = Field(..., description="Path to the audio file")
    seconds: int = Field(..., gt=0, description="Number of seconds to extract")
    is_previous: bool = Field(
        ..., description="If True, extract from end; if False, from beginning"
    )


class PaddedAudioSaveRequest(BaseModel):
    """Request model for saving padded audio."""

    padded_path: Path = Field(..., description="Path where padded audio should be saved")
    processed_speech: torch.Tensor = Field(..., description="Processed audio tensor to save")
    activity_id: str = Field(..., description="Activity identifier for logging")
    phrase_index: int = Field(..., ge=0, description="Phrase index for logging")

    model_config = ConfigDict(arbitrary_types_allowed=True)


class PadAudioRequest(BaseModel):
    """Request model for padding audio in memory."""

    audio_dir: str = Field(..., description="Base audio directory")
    activity_id: str = Field(..., description="Activity ID")
    phrase_index: int = Field(..., ge=0, description="Phrase index (>= 0)")
    padded_seconds: int = Field(
        DEFAULT_PADDED_SECONDS, gt=0, description="Seconds to pad from adjacent phrases"
    )
    save_padded_audio: bool = Field(False, description="Whether to save padded audio to disk")


async def download_tutor_style_audio(
    *, activity_id: str, audio_dir: str, s3_client: ProductionS3Client
) -> bool:
    """Download and process tutor-style audio for a given activity.

    Args:
        activity_id: The unique identifier for the activity.
        audio_dir: The directory where audio files will be downloaded and stored.
        environment: The environment identifier for the source.

    Returns:
        True if the audio was successfully downloaded and processed, False otherwise.

    Raises:
        FileNotFoundError: When required audio files cannot be found.
        Exception: For any other processing errors.
    """
    try:
        return await extract_phrase_slices_tutor_style(
            activity_id=activity_id,
            activity_dir=audio_dir,
            s3_client=s3_client,
            stage_source=False,
            use_audio_dir_as_activities_root=False,
        )
    except FileNotFoundError as e:
        logger.warning(f"Failed to find files for activity {activity_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Error processing activity {activity_id}: {e}")
        return False


def _load_audio_segment(*, request: AudioSegmentRequest) -> torch.Tensor | None:
    """Load and segment audio from a file for padding purposes.

    Args:
        request: Audio segment request containing file_path, seconds, and is_previous flag.

    Returns:
        Audio segment tensor or None if file doesn't exist or loading fails.

    Raises:
        Exception: For audio loading or resampling errors.
    """
    if not Path(request.file_path).exists():
        return None

    try:
        if _is_wav_empty(path=request.file_path):
            return None
        resampled_seg: torch.Tensor | None = _load_and_resample(path=request.file_path)
        if resampled_seg is None:
            return None
        num_samples: int = int(request.seconds * DEFAULT_SAMPLING_RATE)

        return resampled_seg[-num_samples:] if request.is_previous else resampled_seg[:num_samples]
    except Exception as e:
        logger.warning(f"Failed to load audio segment from {request.file_path}: {e}")
        return None


def _save_padded_audio(*, request: PaddedAudioSaveRequest) -> None:
    """Save padded audio to disk.

    Args:
        request: Padded audio save request containing path, audio data, and identifiers.

    Raises:
        Exception: For file system or audio saving errors.
    """
    try:
        activity_dir: Path = Path(request.padded_path).parent
        activity_dir.mkdir(parents=True, exist_ok=True)

        torchaudio.save(
            request.padded_path,
            request.processed_speech.unsqueeze(0),
            DEFAULT_SAMPLING_RATE,
            format="wav",
            bits_per_sample=AUDIO_BITS_PER_SAMPLE,
        )
    except Exception as e:
        logger.warning(
            f"Failed to save padded audio for {request.activity_id}/{request.phrase_index}: {e}"
        )


def pad_audio_in_memory(*, request: PadAudioRequest) -> np.ndarray | None:
    """Load and pad audio in memory using adjacent phrase audio for padding.

    This function follows the same logic as audio_utils.pad_audio but operates
    in memory. It uses adjacent phrase audio for padding instead of zero-padding
    to provide more realistic audio context.

    Args:
        request: Pad audio request containing all necessary parameters.

    Returns:
        Padded audio array or None if loading fails.
    """
    try:
        audio_dir_path: Path = (
            Path(request.audio_dir) / RECONSTITUTED_AUDIO_SUBDIR / DEFAULT_AUDIO_SUBDIR
        )
        padded_path: Path = (
            audio_dir_path
            / f"{request.activity_id}/phrase_{int(request.phrase_index)}{PADDED_AUDIO_SUFFIX}"
        )
        if Path(padded_path).exists():
            logger.info(
                f"Using existing padded audio for activity={request.activity_id} phrase={request.phrase_index}"
            )
            loaded: np.ndarray | None = _try_load_existing_padded_audio(padded_path=padded_path)
            if loaded is not None:
                return loaded
            logger.info(
                f"Existing padded audio empty or invalid; regenerating for activity={request.activity_id} phrase={request.phrase_index}"
            )
        main_path: Path = (
            audio_dir_path
            / f"{request.activity_id}/phrase_{int(request.phrase_index)}{AUDIO_FILE_EXTENSION}"
        )
        prev_path: Path = (
            audio_dir_path
            / f"{request.activity_id}/phrase_{int(request.phrase_index) - 1}{AUDIO_FILE_EXTENSION}"
        )
        next_path: Path = (
            audio_dir_path
            / f"{request.activity_id}/phrase_{int(request.phrase_index) + 1}{AUDIO_FILE_EXTENSION}"
        )
        speech: torch.Tensor | None = _load_main_audio(main_path=main_path)
        if speech is None:
            logger.warning(
                f"Main audio missing or empty for activity={request.activity_id} phrase={request.phrase_index}"
            )
            return None
        processed_speech: torch.Tensor = _concatenate_audio_segments(
            speech=speech,
            prev_path=prev_path,
            next_path=next_path,
            padded_seconds=request.padded_seconds,
        )
        if processed_speech.numel() == 0:
            logger.warning(
                f"Processed padded audio is empty for activity={request.activity_id} phrase={request.phrase_index}"
            )
            return None
        if request.save_padded_audio:
            _save_padded_audio(
                request=PaddedAudioSaveRequest(
                    padded_path=padded_path,
                    processed_speech=processed_speech,
                    activity_id=request.activity_id,
                    phrase_index=request.phrase_index,
                )
            )
        return processed_speech.numpy()
    except Exception as e:
        logger.error(f"Failed to pad audio for {request.activity_id}/{request.phrase_index}: {e}")
        return None


def _try_load_existing_padded_audio(*, padded_path: Path) -> np.ndarray | None:
    """Try to load existing padded audio file.

    Args:
        padded_path: Path to the padded audio file.

    Returns:
        Audio array or None if loading fails.
    """
    try:
        if _is_wav_empty(path=padded_path):
            return None
        speech, sr = _load_audio_file(file_path=padded_path)
        resampler = _get_resampler(orig=sr)
        speech = resampler(speech.squeeze(0))
        if speech.numel() == 0:
            return None
        return speech.numpy()
    except Exception as e:
        logger.warning(f"Failed to load existing padded audio {padded_path}: {e}")
        return None


def _load_main_audio(*, main_path: Path) -> torch.Tensor | None:
    """Load the main audio file.

    Args:
        main_path: Path to the main audio file.

    Returns:
        Audio tensor or None if loading fails.
    """
    if not Path(main_path).exists():
        logger.warning(f"Main audio file not found: {main_path}")
        return None

    try:
        loaded: torch.Tensor | None = _load_and_resample(path=main_path)
        return loaded
    except Exception as e:
        logger.error(f"Failed to load main audio file {main_path}: {e}")
        raise RuntimeError(f"Audio loading failed for {main_path}: {e}") from e


def _concatenate_audio_segments(
    *, speech: torch.Tensor, prev_path: Path, next_path: Path, padded_seconds: int
) -> torch.Tensor:
    """Concatenate main audio with previous and next segments.

    Args:
        speech: Main audio tensor.
        prev_path: Path to the previous audio file.
        next_path: Path to the next audio file.
        padded_seconds: Number of seconds to pad.

    Returns:
        Concatenated audio tensor.
    """
    padded_seconds = max(0, min(int(padded_seconds), 30))
    segments: list[torch.Tensor | None] = [
        _load_audio_segment(
            request=AudioSegmentRequest(
                file_path=prev_path,
                seconds=padded_seconds,
                is_previous=True,
            )
        ),
        speech,
        _load_audio_segment(
            request=AudioSegmentRequest(
                file_path=next_path,
                seconds=padded_seconds,
                is_previous=False,
            )
        ),
    ]

    return torch.cat([seg for seg in segments if seg is not None], dim=0)


def load_complete_audio_in_memory(*, audio_dir: str, activity_id: str) -> np.ndarray | None:
    """Load complete.wav audio file directly into memory.

    This function bypasses the phrase reconstitution logic and loads the complete
    audio file directly from the complete_audio directory.

    Args:
        audio_dir: Base audio directory.
        activity_id: Activity ID.

    Returns:
        Complete audio array or None if loading fails.
    """
    try:
        complete_audio_path: Path = (
            Path(audio_dir) / "complete_audio" / activity_id / "complete.wav"
        )

        if not complete_audio_path.exists():
            logger.warning(f"Complete audio file not found: {complete_audio_path}")
            return None

        try:
            speech, sr = torchaudio.load(str(complete_audio_path), format="wav")
        except Exception as e1:
            logger.warning(f"Method 1 failed: {e1}")
            try:
                speech, sr = _load_audio_file(file_path=complete_audio_path)
            except Exception as e2:
                logger.warning(f"Method 2 failed: {e2}")
                try:
                    import warnings

                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=UserWarning, module="torchaudio")
                        speech, sr = torchaudio.load(str(complete_audio_path))
                except Exception as e3:
                    logger.warning(f"Method 3 failed: {e3}")
                    try:
                        from scipy.io import wavfile

                        sr, speech_data = wavfile.read(str(complete_audio_path))
                        speech = torch.from_numpy(speech_data.astype(np.float32))
                        if len(speech.shape) == 1:
                            speech = speech.unsqueeze(0)
                    except Exception as e4:
                        logger.error(f"All audio loading methods failed: {e1}, {e2}, {e3}, {e4}")
                        return None

        resampler = torchaudio.transforms.Resample(orig_freq=sr, new_freq=DEFAULT_SAMPLING_RATE)
        speech = resampler(speech.squeeze(0))

        logger.info(f"Successfully loaded complete audio for {activity_id}")
        return speech.numpy()

    except Exception as e:
        logger.error(f"Failed to load complete audio for {activity_id}: {e}")
        return None


def prefetch_activity_phrase_audio(*, audio_dir: str, activity_id: str) -> None:
    """Prefetch and cache resampled phrase audio tensors for an activity.

    Args:
        audio_dir: Base audio directory.
        activity_id: Activity ID whose phrases to prefetch.
    """
    try:
        phrase_dir: Path = (
            Path(audio_dir) / RECONSTITUTED_AUDIO_SUBDIR / DEFAULT_AUDIO_SUBDIR / activity_id
        )
        if not phrase_dir.exists():
            return
        for wav_path in sorted(phrase_dir.glob("phrase_*.wav")):
            try:
                _load_and_resample(path=wav_path)
            except (OSError, RuntimeError, AudioProcessingError) as e:
                logger.debug(f"Prefetch failed for {wav_path}: {type(e).__name__}: {e}")
                continue
    except (OSError, RuntimeError) as e:
        logger.warning(f"Prefetch failed for {activity_id}: {type(e).__name__}: {e}")
