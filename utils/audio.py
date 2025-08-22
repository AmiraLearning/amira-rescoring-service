import logging
from pathlib import Path
from typing import Final

import numpy as np
import torch
import torchaudio
from pydantic import BaseModel, Field

from infra.s3_client import BotoClient
from utils.extract_phrases import extract_phrase_slices_tutor_style

DEFAULT_SAMPLING_RATE: Final[int] = 16_000
DEFAULT_PADDED_SECONDS: Final[int] = 3
RECONSTITUTED_AUDIO_SUBDIR: Final[str] = "reconstituted_phrase_audio"
DEFAULT_AUDIO_SUBDIR: Final[str] = "DEFAULT"
PADDED_AUDIO_SUFFIX: Final[str] = "_padded.wav"
AUDIO_FILE_EXTENSION: Final[str] = ".wav"
AUDIO_BITS_PER_SAMPLE: Final[int] = 16


# TODO optimize this
class AudioSegmentRequest(BaseModel):
    """Request model for audio segment loading."""

    file_path: str = Field(..., description="Path to the audio file")
    seconds: int = Field(..., gt=0, description="Number of seconds to extract")
    is_previous: bool = Field(
        ..., description="If True, extract from end; if False, from beginning"
    )


class PaddedAudioSaveRequest(BaseModel):
    """Request model for saving padded audio."""

    padded_path: str = Field(..., description="Path where padded audio should be saved")
    processed_speech: torch.Tensor = Field(
        ..., description="Processed audio tensor to save"
    )
    activity_id: str = Field(..., description="Activity identifier for logging")
    phrase_index: int = Field(..., ge=0, description="Phrase index for logging")

    class Config:
        arbitrary_types_allowed = True


class PadAudioRequest(BaseModel):
    """Request model for padding audio in memory."""

    audio_dir: str = Field(..., description="Base audio directory")
    activity_id: str = Field(..., description="Activity ID")
    phrase_index: int = Field(..., ge=0, le=11, description="Phrase index (0-11)")
    padded_seconds: int = Field(
        DEFAULT_PADDED_SECONDS, gt=0, description="Seconds to pad from adjacent phrases"
    )
    save_padded_audio: bool = Field(
        False, description="Whether to save padded audio to disk"
    )


def download_tutor_style_audio(
    *, activity_id: str, audio_dir: str, s3_client: BotoClient
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
        return extract_phrase_slices_tutor_style(
            activity_id=activity_id,
            activity_dir=audio_dir,
            s3_client=s3_client,
            stage_source=False,
            use_audio_dir_as_activities_root=False,
        )
    except FileNotFoundError as e:
        logging.warning(f"Failed to find files for activity {activity_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"Error processing activity {activity_id}: {e}")
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
        seg: torch.Tensor
        seg_sr: int

        seg, seg_sr = torchaudio.load(request.file_path)
        seg_resampler: torchaudio.transforms.Resample = torchaudio.transforms.Resample(
            orig_freq=seg_sr, new_freq=DEFAULT_SAMPLING_RATE
        )
        resampled_seg: torch.Tensor = seg_resampler(seg.squeeze(0))
        num_samples: int = int(request.seconds * DEFAULT_SAMPLING_RATE)

        return (
            resampled_seg[-num_samples:]
            if request.is_previous
            else resampled_seg[:num_samples]
        )
    except Exception as e:
        logging.warning(f"Failed to load audio segment from {request.file_path}: {e}")
        return None


def _save_padded_audio(*, request: PaddedAudioSaveRequest) -> None:
    """Save padded audio to disk.

    Args:
        request: Padded audio save request containing path, audio data, and identifiers.

    Raises:
        Exception: For file system or audio saving errors.
    """
    try:
        activity_dir: str = Path(request.padded_path).parent
        activity_dir.mkdir(parents=True, exist_ok=True)

        torchaudio.save(
            request.padded_path,
            request.processed_speech.unsqueeze(0),
            DEFAULT_SAMPLING_RATE,
            format="wav",
            bits_per_sample=AUDIO_BITS_PER_SAMPLE,
        )
    except Exception as e:
        logging.warning(
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
            return _try_load_existing_padded_audio(padded_path=padded_path)

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
            return None

        processed_speech: torch.Tensor = _concatenate_audio_segments(
            speech=speech,
            prev_path=prev_path,
            next_path=next_path,
            padded_seconds=request.padded_seconds,
        )

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
        logging.error(
            f"Failed to pad audio for {request.activity_id}/{request.phrase_index}: {e}"
        )
        return None


def _try_load_existing_padded_audio(*, padded_path: Path) -> np.ndarray | None:
    """Try to load existing padded audio file.

    Args:
        padded_path: Path to the padded audio file.

    Returns:
        Audio array or None if loading fails.
    """
    try:
        speech, sr = torchaudio.load(padded_path)
        resampler: torchaudio.transforms.Resample = torchaudio.transforms.Resample(
            orig_freq=sr, new_freq=DEFAULT_SAMPLING_RATE
        )
        speech = resampler(speech.squeeze(0))
        return speech.numpy()
    except Exception as e:
        logging.warning(f"Failed to load existing padded audio {padded_path}: {e}")
        return None


def _load_main_audio(*, main_path: Path) -> torch.Tensor | None:
    """Load the main audio file.

    Args:
        main_path: Path to the main audio file.

    Returns:
        Audio tensor or None if loading fails.
    """
    if not Path(main_path).exists():
        logging.warning(f"Main audio file not found: {main_path}")
        return None

    try:
        speech, sr = torchaudio.load(main_path)
        resampler: torchaudio.transforms.Resample = torchaudio.transforms.Resample(
            orig_freq=sr, new_freq=DEFAULT_SAMPLING_RATE
        )
        return resampler(speech.squeeze(0))
    except Exception as e:
        logging.error(f"Failed to load main audio file {main_path}: {e}")
        return None


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
