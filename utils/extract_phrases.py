import urllib.parse
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from infra.s3_client import ProductionS3Client
from utils.phrase_slicing import RECONSTITUTED_PHRASE_AUDIO, PhraseSlicer


class AudioPaths(BaseModel):
    stage_activity_id: str
    dataset_name_suffix: str = Field(default="DEFAULT")
    dataset_dir_full_path: Path = Field(default_factory=Path)
    activity_dir_full_path: Path = Field(default_factory=Path)


def _resolve_paths(
    *,
    activity_id: str,
    activity_dir: str,
    replay_suffix: str | None,
    dataset_name: str | None,
    use_audio_dir_as_activities_root: bool,
) -> AudioPaths:
    """Resolve all paths needed for phrase extraction.

    Args:
        activity_id: Activity identifier.
        activity_dir: Local base directory for audio data.
        replay_suffix: Alternate suffix for production IDs.
        dataset_name: Dataset name.
        use_audio_dir_as_activities_root: Treat activity_dir as activities root.

    Returns:
        AudioPaths object containing all paths
    """
    # Handle replay suffix: replace last 4 chars of activity_id with replay_suffix
    # This logic handles production ID transformations correctly
    stage_activity_id: str = f"{activity_id[:-4]}{replay_suffix}" if replay_suffix else activity_id

    # Validate that activity_id transformation makes sense
    if replay_suffix and len(activity_id) < 4:
        raise ValueError(f"Activity ID '{activity_id}' too short for replay suffix transformation")

    if RECONSTITUTED_PHRASE_AUDIO in activity_dir:
        root_dir_str: str = activity_dir.split(RECONSTITUTED_PHRASE_AUDIO)[0]
        root_dir: Path = Path(root_dir_str)
        dataset_name_suffix: str = (
            activity_dir.split(RECONSTITUTED_PHRASE_AUDIO)[-1].strip("/")
            if dataset_name is None
            else dataset_name
        )
    else:
        root_dir = Path(activity_dir)
        dataset_name_suffix = dataset_name if dataset_name is not None else "DEFAULT"

    if use_audio_dir_as_activities_root:
        dataset_dir_full_path: Path = Path(activity_dir)
    else:
        dataset_dir_full_path = Path(root_dir) / RECONSTITUTED_PHRASE_AUDIO / dataset_name_suffix

    activity_dir_full_path: Path = dataset_dir_full_path / stage_activity_id
    return AudioPaths(
        stage_activity_id=stage_activity_id,
        dataset_name_suffix=dataset_name_suffix,
        dataset_dir_full_path=dataset_dir_full_path,
        activity_dir_full_path=activity_dir_full_path,
    )


async def _upload_phrase_files_to_s3(
    *,
    activity_dir_full_path: Path,
    audio_s3_root: Path,
    dataset_name_suffix: str,
    stage_activity_id: str,
    s3_client: ProductionS3Client,
) -> None:
    """Upload phrase files to S3.

    Args:
        activity_dir_full_path: Path to the activity directory.
        audio_s3_root: S3 URL root for uploads.
        dataset_name_suffix: Dataset name suffix.
        stage_activity_id: Activity identifier with stage suffix.
        s3: Optional boto3 S3 client.
    """
    parsed_url = urllib.parse.urlparse(str(audio_s3_root))
    bucket: str = parsed_url.netloc
    base_path_str: str = parsed_url.path.strip("/").split(RECONSTITUTED_PHRASE_AUDIO)[0]
    base_path: Path = Path(base_path_str)
    activity_dir_full_path.mkdir(parents=True, exist_ok=True)

    upload_operations = []
    for wav_file in activity_dir_full_path.iterdir():
        if not str(wav_file).endswith(".wav"):
            continue
        key: str = str(
            base_path
            / RECONSTITUTED_PHRASE_AUDIO
            / dataset_name_suffix
            / stage_activity_id
            / wav_file.name
        )
        filename: str = str(activity_dir_full_path / wav_file.name)
        upload_operations.append((filename, bucket, key))

    if upload_operations:
        try:
            results = await s3_client.upload_files_batch(upload_operations)
            for result in results:
                if not result.success:
                    logger.warning(f"Failed to upload: {result.error}")
        except Exception as e:
            logger.warning(f"Batch upload failed: {e}")


async def extract_phrase_slices_tutor_style(
    *,
    activity_id: str,
    activity_dir: str,
    replay_suffix: str | None = None,
    dataset_name: str | None = None,
    audio_s3_root: Path | None = None,
    s3_client: ProductionS3Client,
    stage_source: bool = False,
    use_audio_dir_as_activities_root: bool = False,
) -> bool:
    """Ensure phrase-sliced audio exists locally and optionally upload to S3.

    Args:
        activity_id: Activity identifier.
        activity_dir: Local base directory for audio data.
        replay_suffix: Alternate suffix for production IDs.
        dataset_name: Dataset name; defaults to DEFAULT if not provided.
        audio_s3_root: S3 URL root for uploads. Must be a valid URL.
        s3_client: boto3 S3 client.
        stage_source: Whether the source is from staging.
        use_audio_dir_as_activities_root: Treat activity_dir as activities root.

    Returns:
        True on success.
    """
    audio_paths: AudioPaths = _resolve_paths(
        activity_id=activity_id,
        activity_dir=activity_dir,
        replay_suffix=replay_suffix,
        dataset_name=dataset_name,
        use_audio_dir_as_activities_root=use_audio_dir_as_activities_root,
    )

    logger.info(
        f"extract_phrase_slices_tutor_style activity_id {audio_paths.stage_activity_id} activity_dir {activity_dir}"
    )

    audio_paths.dataset_dir_full_path.mkdir(parents=True, exist_ok=True)

    existing_folders: list[str] = [
        str(folder.name)
        for folder in audio_paths.dataset_dir_full_path.iterdir()
        if folder.is_dir()
    ]

    activity_folder: Path = audio_paths.dataset_dir_full_path / audio_paths.stage_activity_id
    needs_processing: bool = True

    if activity_folder.exists():
        phrase_files: list[Path] = list(activity_folder.glob("phrase_*.wav"))
        if phrase_files:
            logger.info(
                f"Found {len(phrase_files)} existing phrase files for {audio_paths.stage_activity_id}, skipping regeneration"
            )
            needs_processing = False
        else:
            logger.warning(
                f"Folder exists but no phrase files found for {audio_paths.stage_activity_id}, will regenerate"
            )
    elif activity_id in existing_folders:
        old_folder: Path = audio_paths.dataset_dir_full_path / activity_id
        phrase_files = list(old_folder.glob("phrase_*.wav"))
        if phrase_files:
            logger.info(
                f"Found {len(phrase_files)} existing phrase files for {activity_id}, renaming folder"
            )
            old_folder.rename(activity_folder)
            needs_processing = False
        else:
            logger.warning(
                f"Folder {activity_id} exists but no phrase files found, will regenerate"
            )

    if needs_processing:
        await PhraseSlicer(
            destination_path=str(audio_paths.dataset_dir_full_path),
            s3_client=s3_client,
            stage_source=stage_source,
        ).process_activity_into_phrase_sliced_audio(
            activity_id=activity_id, replay_suffix=replay_suffix
        )

        if audio_s3_root is not None:
            await _upload_phrase_files_to_s3(
                activity_dir_full_path=audio_paths.activity_dir_full_path,
                audio_s3_root=audio_s3_root,
                dataset_name_suffix=audio_paths.dataset_name_suffix,
                stage_activity_id=audio_paths.stage_activity_id,
                s3_client=s3_client,
            )

    return True
