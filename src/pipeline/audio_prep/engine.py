from pathlib import Path

import numpy as np
from loguru import logger

from infra.s3_client import (
    HighPerformanceS3Config,
    ProductionS3Client,
    get_global_s3_client,
)
from utils.audio import (
    DEFAULT_AUDIO_SUBDIR,
    DEFAULT_SAMPLING_RATE,
    RECONSTITUTED_AUDIO_SUBDIR,
    PadAudioRequest,
    download_complete_audio_from_s3,
    download_tutor_style_audio,
    pad_audio_in_memory,
    prefetch_activity_phrase_audio,
)
from utils.config import PipelineConfig

from .models import (
    ActivityInput,
    ActivityOutput,
    PhraseInput,
    ProcessedPhraseOutput,
)


class AudioPreparationEngine:
    """Audio preparation engine.

    Attributes:
        config: The pipeline configuration.
        s3_client: The S3 client.
    """

    def __init__(self, *, config: PipelineConfig, s3_client: ProductionS3Client | None = None):
        """Initialize the audio preparation engine.

        Args:
            config: The pipeline configuration.
            s3_client: The S3 client.

        Returns:
            None

        Raises:
            ValueError: If config is None or invalid
            Exception: If S3 client initialization fails
        """
        if config is None:
            raise ValueError("Configuration parameter is required")

        if not hasattr(config, "audio") or config.audio is None:
            raise ValueError("Configuration must contain valid audio settings")
        self._config = config
        self._audio_base_dir: str = str(config.audio.audio_dir)
        self._environment: str = config.aws.audio_env
        self._result_dir: str = config.result.output_dir
        self._padded_seconds: int = config.audio.padded_seconds

        self._save_padded_audio: bool = config.audio.save_padded_audio and config.result.audit_mode
        hp_config = HighPerformanceS3Config(
            aws_profile=config.aws.aws_profile,
            aws_region=config.aws.aws_region,
        )
        self._s3_client: ProductionS3Client = s3_client or get_global_s3_client(config=hp_config)

    async def prepare_activity_audio(self, *, activity_input: ActivityInput) -> ActivityOutput:
        """Prepare the activity audio.

        Args:
            activity_input: The activity input.

        Returns:
            ActivityOutput: The activity output with processed phrases.

        Raises:
            RuntimeError: If audio download fails or critical processing errors occur.
        """
        activity_id: str = activity_input.activityId
        phrases: list[PhraseInput] = activity_input.phrases
        output: ActivityOutput = ActivityOutput(activityId=activity_id)

        logger.info(f"Processing activity {activity_id}")

        try:
            audio_available = await self._ensure_activity_audio_downloaded(activity_id=activity_id)
            if not audio_available:
                output.error_message = f"No audio available for activity {activity_id}"
                logger.warning(output.error_message)
                return output
        except Exception as e:
            error_msg = f"Failed to download audio for activity {activity_id}: {e}"
            logger.error(error_msg)
            output.error_message = error_msg
            return output

        prefetch_activity_phrase_audio(audio_dir=self._audio_base_dir, activity_id=activity_id)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers: int = 8

        def _work(
            phrase: PhraseInput,
        ) -> tuple[int, ProcessedPhraseOutput | None, str | None]:
            idx: int = phrase.phraseIndex
            try:
                logger.info(f"Processing phrase {idx} for activity {activity_id}")
                res = self._process_single_phrase(activity_id=activity_id, phrase_data=phrase)
                return idx, res, None
            except FileNotFoundError as e:
                error_msg = f"Audio file not found for phrase {idx}: {e}"
                logger.error(error_msg)
                return idx, None, error_msg
            except ValueError as e:
                error_msg = f"Invalid audio data for phrase {idx}: {e}"
                logger.error(error_msg)
                return idx, None, error_msg
            except Exception as e:
                error_msg = f"Unexpected error processing phrase {idx}: {e}"
                logger.error(error_msg)
                return idx, None, error_msg

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_work, p): p.phraseIndex for p in phrases}
            for fut in as_completed(futures):
                phrase_index = futures[fut]
                idx, processed_phrase, err = fut.result()
                if err is not None:
                    output.phrases_failed += 1
                    logger.error(f"Error processing phrase {phrase_index}: {err}")
                elif processed_phrase:
                    output.phrases.append(processed_phrase)
                    output.phrases_processed += 1
                    logger.info(f"Successfully processed phrase {phrase_index}")
                else:
                    output.phrases_failed += 1
                    logger.warning(f"Failed to process phrase {phrase_index} - no audio returned")

        if output.phrases_processed > 0:
            output.success = True
            logger.info(
                f"Activity {activity_id} prepared with {output.phrases_processed}/{len(phrases)} phrases"
            )
        else:
            output.error_message = f"No valid audio found for activity {activity_id}"
            logger.warning(f"Activity {activity_id} has no valid audio - all audio files are empty")
            output.success = False

        logger.info(
            f"Activity {activity_id} prepared with {output.phrases_processed}/{len(phrases)} phrases"
        )
        return output

    async def close(self) -> None:
        """Close underlying resources like the S3 client sessions.

        This method ensures graceful cleanup of S3 connections and should
        be called when the engine is no longer needed.
        """
        try:
            await self._s3_client.close()
            logger.debug("S3 client closed successfully")
        except AttributeError:
            logger.debug("S3 client was not initialized or already closed")
        except Exception as e:
            logger.warning(f"Failed to close S3 client cleanly: {e}")

    async def prepare_activity_audio_with_complete(
        self, *, activity_input: ActivityInput
    ) -> ActivityOutput:
        """Prepare activity audio using complete.wav (bypasses phrase reconstitution).

        Args:
            activity_input: The activity input.

        Returns:
            ActivityOutput: The activity output with processed phrases.
        """
        activity_id: str = activity_input.activityId
        phrases: list[PhraseInput] = activity_input.phrases
        output: ActivityOutput = ActivityOutput(activityId=activity_id)

        logger.info(f"Processing activity {activity_id} with complete audio")

        try:
            audio_available = await self._ensure_complete_audio_downloaded(activity_id=activity_id)
            if not audio_available:
                logger.warning(
                    f"No complete audio available for activity {activity_id}; falling back to phrase reconstitution path"
                )
                return await self.prepare_activity_audio(activity_input=activity_input)

            from utils.audio import download_tutor_style_audio

            logger.info(f"Starting phrase slicing for {activity_id}")
            slice_success = await download_tutor_style_audio(
                activity_id=activity_id,
                audio_dir=self._audio_base_dir,
                s3_client=self._s3_client,
            )

            if slice_success:
                logger.info(f"Phrase slicing successful for {activity_id}")
            else:
                logger.error(f"Phrase slicing FAILED for {activity_id}")

        except Exception as e:
            error_msg = f"Failed to download complete audio for activity {activity_id}: {e}"
            logger.error(error_msg)
            output.error_message = error_msg
            return output

        for phrase_data in phrases:
            phrase_index: int = phrase_data.phraseIndex
            try:
                logger.info(
                    f"Processing phrase {phrase_index} with complete audio for activity {activity_id}"
                )
                processed_phrase = self._process_single_phrase_with_complete_audio(
                    activity_id=activity_id, phrase_data=phrase_data
                )

                if processed_phrase:
                    output.phrases.append(processed_phrase)
                    output.phrases_processed += 1
                    logger.info(f"Successfully processed phrase {phrase_index} with complete audio")
                else:
                    output.phrases_failed += 1
                    logger.warning(
                        f"Failed to process phrase {phrase_index} - no complete audio returned"
                    )
            except Exception as e:
                output.phrases_failed += 1
                error_msg = f"Error processing phrase {phrase_index} with complete audio: {e}"
                logger.error(error_msg)

        if output.phrases_processed > 0:
            output.success = True
            logger.info(
                f"Activity {activity_id} prepared with complete audio: {output.phrases_processed}/{len(phrases)} phrases"
            )
        else:
            output.error_message = f"No valid complete audio found for activity {activity_id}"
            logger.warning(f"Activity {activity_id} has no valid complete audio")
            output.success = True

        return output

    async def _ensure_activity_audio_downloaded(self, *, activity_id: str) -> bool:
        """Ensure the activity audio is downloaded.

        Args:
            activity_id: The activity ID.

        Returns:
            bool: True if the audio is downloaded, False otherwise.
        """

        def _has_valid_phrase_audio(*, activity_audio_dir: Path) -> bool:
            try:
                if not activity_audio_dir.exists():
                    return False
                phrase_files = sorted(activity_audio_dir.glob("phrase_*.wav"))
                if not phrase_files:
                    return False
                for f in phrase_files[:2]:
                    try:
                        if f.stat().st_size > 44:
                            return True
                    except Exception:
                        continue
                return False
            except Exception:
                return False

        audio_dir_path: Path = Path(
            self._audio_base_dir, RECONSTITUTED_AUDIO_SUBDIR, DEFAULT_AUDIO_SUBDIR
        )
        activity_audio_dir: Path = Path(audio_dir_path, activity_id)
        if not _has_valid_phrase_audio(activity_audio_dir=activity_audio_dir):
            logger.info(f"Downloading audio for activity {activity_id}")
            download_success = await download_tutor_style_audio(
                activity_id=activity_id,
                audio_dir=self._audio_base_dir,
                s3_client=self._s3_client,
            )
            if download_success:
                logger.info(f"Audio downloaded for {activity_id}")
            else:
                logger.warning(f"Download failed for {activity_id}")
            if not _has_valid_phrase_audio(activity_audio_dir=activity_audio_dir):
                logger.warning(f"Skipping activity {activity_id} (no valid audio)")
                return False
        else:
            logger.info(f"Audio already exists and is valid for {activity_id}")
        return True

    async def _ensure_complete_audio_downloaded(self, *, activity_id: str) -> bool:
        """Ensure complete.wav is downloaded from S3.

        Args:
            activity_id: The activity ID.

        Returns:
            bool: True if complete.wav is downloaded, False otherwise.
        """
        complete_audio_path = (
            Path(self._audio_base_dir) / "complete_audio" / activity_id / "complete.wav"
        )

        if complete_audio_path.exists():
            try:
                if complete_audio_path.stat().st_size > 44:
                    logger.info(f"Complete audio already exists for {activity_id}")
                    return True
                logger.warning(
                    f"Complete audio present but empty for {activity_id}; re-downloading"
                )
            except Exception:
                logger.warning(f"Unable to stat complete audio for {activity_id}; re-downloading")

        logger.info(f"Downloading complete audio for activity {activity_id}")
        download_success = await download_complete_audio_from_s3(
            activity_id=activity_id,
            audio_dir=self._audio_base_dir,
            s3_client=self._s3_client,
        )

        if download_success:
            logger.info(f"Complete audio downloaded for {activity_id}")
            return True
        else:
            logger.warning(f"Complete audio download failed for {activity_id}")
            return False

    def _process_single_phrase_with_complete_audio(
        self, *, activity_id: str, phrase_data: PhraseInput
    ) -> ProcessedPhraseOutput | None:
        """Process a single phrase using complete audio approach.

        This loads the individual phrase slice (not the complete audio) since
        phrase slicing has already been done by extract_phrase_slices_tutor_style.

        Args:
            activity_id: The activity ID.
            phrase_data: The phrase data.

        Returns:
            ProcessedPhraseOutput: The processed phrase output.
        """
        logger.info(
            f"Processing phrase {phrase_data.phraseIndex} with complete audio for {activity_id}"
        )

        # Use the same logic as regular phrase processing to load the individual phrase slice
        request: PadAudioRequest = PadAudioRequest(
            audio_dir=self._audio_base_dir,
            activity_id=activity_id,
            phrase_index=phrase_data.phraseIndex,
            padded_seconds=self._padded_seconds,
            save_padded_audio=self._save_padded_audio,
        )
        audio_array: np.ndarray | None = pad_audio_in_memory(request=request)

        if audio_array is not None and len(audio_array) > 0:
            return ProcessedPhraseOutput(
                activityId=activity_id,
                phraseIndex=phrase_data.phraseIndex,
                speech=audio_array,
                sampling_rate=DEFAULT_SAMPLING_RATE,
                reference_phonemes=phrase_data.reference_phonemes,
                expected_text=phrase_data.expected_text,
                storyId=phrase_data.storyId,
                studentId=phrase_data.studentId,
            )
        else:
            logger.warning(
                f"Failed to load phrase audio for {activity_id} phrase {phrase_data.phraseIndex}"
            )
            return None

    def _process_single_phrase(
        self, *, activity_id: str, phrase_data: PhraseInput
    ) -> ProcessedPhraseOutput | None:
        """Process a single phrase.

        Args:
            activity_id: The activity ID.
            phrase_data: The phrase data.

        Returns:
            ProcessedPhraseOutput: The processed phrase output.
        """
        logger.info(f"Entering _process_single_phrase for phrase {phrase_data.phraseIndex}")
        request: PadAudioRequest = PadAudioRequest(
            audio_dir=self._audio_base_dir,
            activity_id=activity_id,
            phrase_index=phrase_data.phraseIndex,
            padded_seconds=self._padded_seconds,
            save_padded_audio=self._save_padded_audio,
        )
        audio_array: np.ndarray | None = pad_audio_in_memory(request=request)
        if audio_array is not None and len(audio_array) > 0:
            return ProcessedPhraseOutput(
                activityId=activity_id,
                phraseIndex=phrase_data.phraseIndex,
                speech=audio_array,
                sampling_rate=DEFAULT_SAMPLING_RATE,
                reference_phonemes=phrase_data.reference_phonemes,
                expected_text=phrase_data.expected_text,
                storyId=phrase_data.storyId,
                studentId=phrase_data.studentId,
            )
        else:
            logger.warning(
                f"Failed to process phrase {phrase_data.phraseIndex} - no audio returned"
            )
            return None
