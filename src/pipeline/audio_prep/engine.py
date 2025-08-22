from __future__ import annotations

import numpy as np
from pathlib import Path
from loguru import logger
from utils.config import PipelineConfig
from utils.audio import (
    download_tutor_style_audio,
    pad_audio_in_memory,
    DEFAULT_SAMPLING_RATE,
)
from infra.s3_client import ProductionS3Client, HighPerformanceS3Config
from .models import (
    PhraseInput,
    ActivityInput,
    ProcessedPhraseOutput,
    ActivityOutput,
)
from utils.audio import PadAudioRequest


class AudioPreparationEngine:
    """Audio preparation engine.

    Attributes:
        config: The pipeline configuration.
        s3_client: The S3 client.
    """

    def __init__(
        self, *, config: PipelineConfig, s3_client: ProductionS3Client | None = None
    ):
        """Initialize the audio preparation engine.

        Args:
            config: The pipeline configuration.
            s3_client: The S3 client.

        Returns:
            None
        """
        self._config = config
        self._audio_base_dir: str = config.audio.audio_dir
        self._environment: str = config.aws.audio_env
        self._result_dir: str = config.result.output_dir
        self._padded_seconds: int = config.audio.padded_seconds
        self._save_padded_audio: bool = config.audio.save_padded_audio
        self._s3_client: ProductionS3Client = s3_client or ProductionS3Client(
            config=HighPerformanceS3Config(
                aws_profile=config.aws.aws_profile,
                aws_region=config.aws.aws_region,
            )
        )

    def prepare_activity_audio(
        self, *, activity_input: ActivityInput
    ) -> ActivityOutput:
        """Prepare the activity audio.

        Args:
            activity_input: The activity input.

        Returns:
            ActivityOutput: The activity output.
        """
        activity_id: str = activity_input.activityId
        phrases: list[PhraseInput] = activity_input.phrases

        logger.info(f"Processing activity {activity_id}")
        output = ActivityOutput(activityId=activity_id)
        try:
            try:
                _ = self._ensure_activity_audio_downloaded(activity_id=activity_id)
            except Exception as e:
                logger.warning(
                    f"Failed to download audio for activity {activity_id}: {e}"
                )
                raise RuntimeError(
                    f"Failed to download audio for activity {activity_id}: {e}"
                )

            for phrase_data in phrases:
                try:
                    processed_phrase = self._process_single_phrase(
                        activity_id=activity_id, phrase_data=phrase_data
                    )
                    if processed_phrase:
                        output.phrases.append(processed_phrase)
                        output.phrases_processed += 1
                    else:
                        output.phrases_failed += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to process {activity_id}_{phrase_data.phraseIndex}: {e}"
                    )
                    output.phrases_failed += 1

            if output.phrases_processed > 0:
                output.success = True
                logger.info(
                    f"Activity {activity_id} prepared with {output.phrases_processed}/{len(phrases)} phrases"
                )
            else:
                output.error_message = (
                    f"No valid audio found for activity {activity_id}"
                )
                raise RuntimeError(output.error_message)
        except Exception as e:
            error_msg = f"Failed to prepare activity {activity_id}: {str(e)}"
            logger.error(f"{error_msg}")
            raise RuntimeError(error_msg)
        return output

    def _ensure_activity_audio_downloaded(self, *, activity_id: str) -> bool:
        """Ensure the activity audio is downloaded.

        Args:
            activity_id: The activity ID.

        Returns:
            bool: True if the audio is downloaded, False otherwise.
        """
        audio_dir_path: str = Path(
            self._audio_base_dir, "reconstituted_phrase_audio", "DEFAULT"
        )
        activity_audio_dir: str = Path(audio_dir_path, activity_id)
        if not Path(activity_audio_dir).exists():
            logger.info(f"Downloading audio for activity {activity_id}")
            download_success = download_tutor_style_audio(
                activity_id, self._audio_base_dir, self._environment, self._s3_client
            )
            if download_success:
                logger.info(f"Audio downloaded for {activity_id}")
            else:
                logger.warning(f"Download failed for {activity_id}")
            if not Path(activity_audio_dir).exists():
                logger.warning(f"Skipping activity {activity_id} (no valid audio)")
                return False
        else:
            logger.debug(f"Audio already exists for {activity_id}")
        return True

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
        return None
