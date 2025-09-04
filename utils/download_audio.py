"""S3 helpers for downloading and preparing tutor-style phrase audio.

This module provides small, focused utilities with clear typing and
keyword-only arguments for safer, more readable usage.
"""

import urllib.parse
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from infra.s3_client import HighPerformanceS3Config, ProductionS3Client, S3UploadRequest
from utils.config import RECONSTITUTED_PHRASE_AUDIO
from utils.phrase_slicing import PhraseSlicer
from utils.s3_audio_operations import (
    bucket_for as _bucket_for,
)
from utils.s3_audio_operations import (
    get_segment_file_names as _get_segment_files,
)


def resolve_bucket(stage_source: bool) -> str:
    """Resolve S3 bucket name based on environment (delegates to shared helper)."""
    return _bucket_for(stage_source=stage_source)


class DatasetLayout(BaseModel):
    root_dir: Path
    dataset_dir: Path
    dataset_suffix: str


def effective_activity_id(*, activity_id: str, replay_suffix: str | None) -> str:
    return f"{activity_id[:-4]}{replay_suffix}" if replay_suffix else activity_id


def resolve_dataset_layout(
    *, audio_path: Path, dataset_name: str, use_audio_dir_as_root: bool
) -> DatasetLayout:
    """Resolve the dataset layout for a given audio directory

    Args:
        audio_dir: Path to the local audio directory
        dataset_name: dataset name to use
        use_audio_dir_as_root: whether to use audio_dir as the root for activities

    Returns:
        The resolved dataset layout
    """
    if RECONSTITUTED_PHRASE_AUDIO in str(audio_path):
        resolved_root_dir: Path = Path(str(audio_path).split(RECONSTITUTED_PHRASE_AUDIO)[0])
        resolved_dataset_suffix = str(audio_path).split(RECONSTITUTED_PHRASE_AUDIO)[-1].strip("/")
    else:
        resolved_root_dir = audio_path
        resolved_dataset_suffix = dataset_name

    dataset_dir: Path = (
        audio_path
        if use_audio_dir_as_root
        else resolved_root_dir / RECONSTITUTED_PHRASE_AUDIO / resolved_dataset_suffix
    )
    return DatasetLayout(
        root_dir=resolved_root_dir,
        dataset_dir=dataset_dir,
        dataset_suffix=resolved_dataset_suffix,
    )


def iter_wav_files(*, directory: Path) -> list[Path]:
    """Iterate over all WAV files in a given directory

    Args:
        directory: Path to the directory to iterate over

    Returns:
        List of Path objects representing the WAV files
    """
    return [f for f in directory.iterdir() if f.is_file() and f.suffix == ".wav"]


async def get_segment_file_names(
    *, activity_id: str, s3_client: ProductionS3Client, stage_source: bool = False
) -> list[str]:
    """Get the file names of all the audio segments for an activity (shared helper)."""
    return await _get_segment_files(
        activity_id=activity_id, s3_client=s3_client, stage_source=stage_source
    )


async def download_tutor_style_audio(
    *,
    activity_id: str,
    audio_path: Path,
    s3_client: ProductionS3Client,
    dataset_name: str,
    audio_s3_root: Path,
    replay_suffix: str | None = None,
    stage_source: bool = False,
    use_audio_dir_as_root: bool = False,
) -> bool:
    """
    Downloads and prepares phrase audio files for a specific activity.

    First attempts to download pre-sliced phrase audio from S3. If not available,
    downloads and slices the audio locally, then uploads the sliced files back to S3.

    Args:
        activity_id: Activity ID to process
        audio_dir: Path to the local audio directory
        s3_client: S3 client to use
        dataset_name: Dataset name to use
        audio_s3_root: S3 endpoint for phrase audio files
        replay_suffix: Four-letter replay suffix for production IDs
        stage_source: Whether the source activity is from staging
        use_audio_dir_as_root: whether to use audio_dir as the root for activities

    Returns:
        True if the operation was successful
    """
    eff_act_id: str = effective_activity_id(activity_id=activity_id, replay_suffix=replay_suffix)

    logger.info(f"Downloading audio for activity {eff_act_id} to {audio_path}")

    layout: DatasetLayout = resolve_dataset_layout(
        audio_path=audio_path,
        dataset_name=dataset_name,
        use_audio_dir_as_root=use_audio_dir_as_root,
    )

    activity_dir: Path = layout.dataset_dir / eff_act_id
    layout.dataset_dir.mkdir(parents=True, exist_ok=True)

    existing_folders: set[str] = {
        folder.name for folder in layout.dataset_dir.iterdir() if folder.is_dir()
    }

    if eff_act_id not in existing_folders:
        if activity_id in existing_folders:
            Path(layout.dataset_dir, activity_id).rename(Path(layout.dataset_dir, eff_act_id))
        else:
            tuned_client = s3_client
            if tuned_client is None:
                cfg = HighPerformanceS3Config()
                tuned_client = ProductionS3Client(config=cfg)
            await PhraseSlicer(
                destination_path=str(layout.dataset_dir),
                s3_client=tuned_client,
                stage_source=stage_source,
            ).process_activity_into_phrase_sliced_audio(
                activity_id=activity_id,
                replay_suffix=replay_suffix,
                keep_intermediaries=False,
            )

            if audio_s3_root:
                await _upload_sliced_audio_to_s3(
                    audio_s3_root=audio_s3_root,
                    activity_dir=activity_dir,
                    dataset_suffix=layout.dataset_suffix,
                    effective_activity_id=eff_act_id,
                    s3_client=s3_client,
                )

    return True


async def _upload_sliced_audio_to_s3(
    *,
    audio_s3_root: Path,
    activity_dir: Path,
    dataset_suffix: str,
    effective_activity_id: str,
    s3_client: ProductionS3Client,
) -> None:
    """
    Uploads sliced audio files to S3.

    Args:
        audio_s3_root: S3 root URL
        activity_dir: Path to the activity directory containing WAV files
        dataset_suffix: Dataset name suffix
        effective_activity_id: Activity ID to use for the S3 path
        s3_client: S3 client to use for uploads
    """
    parsed_url = urllib.parse.urlparse(str(audio_s3_root))
    bucket_name: str = parsed_url.netloc

    activity_dir.mkdir(parents=True, exist_ok=True)

    upload_operations = []
    for wav_file in iter_wav_files(directory=activity_dir):
        base_path: str = parsed_url.path.strip("/").split(RECONSTITUTED_PHRASE_AUDIO)[0]
        s3_path: Path = (
            Path(base_path)
            / RECONSTITUTED_PHRASE_AUDIO
            / dataset_suffix
            / effective_activity_id
            / wav_file.name
        )

        upload_operations.append(
            S3UploadRequest(local_path=str(wav_file), bucket=bucket_name, key=str(s3_path))
        )

    if upload_operations:
        try:
            results = await s3_client.upload_files_batch(upload_operations)
            for result in results:
                if not result.success:
                    logger.warning(f"Failed to upload: {result.error}")
        except Exception as e:
            logger.warning(f"Batch upload failed: {e}")
