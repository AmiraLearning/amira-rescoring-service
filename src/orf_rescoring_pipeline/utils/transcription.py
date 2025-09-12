"""Transcription functionality for the offline pipeline.

This module handles automatic speech recognition (ASR) using multiple providers
including Deepgram, Kaldi, and Wav2Vec. It provides clean interfaces for
transcribing audio data with word-level timing information essential for
the alignment process.
"""

import asyncio
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import requests
import soundfile as sf
from amira_pyutils.services.kaldi import KaldiClient
from amira_pyutils.services.w2v_client import W2VStackClient
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from orf_rescoring_pipeline.models import Activity, TranscriptItem, WordItem

logger = logging.getLogger(__name__)

# Constants
DEFAULT_DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_API_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = "mip_opt_out=true&language=en"

GENERAL_KALDI_URL = "https://kaldi-shard-general-1.stage.amira.cloud/"
PARTNER_KALDI_URL = "https://kaldi-shard-partner-1.stage.amira.cloud/"
KALDI_GRAPH_ID = "asr-am2-85-amira-cmu"

W2V_ENDPOINT = "https://wav2vec-assessment.stage.amira.cloud"
W2V_MAX_PHRASE_LENGTH_MS = 70_000
W2V_TARGET_SAMPLE_RATE = 16000

RETRY_MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 1.0

TRANSCRIPT_DIR_NAME = "deepgram_transcription_and_slicing_data"


class DeepgramASRClient:
    """Client for Deepgram automatic speech recognition API.

    Provides a clean interface for transcribing audio data using Deepgram's API
    with built-in error handling and caching capabilities.
    """

    def __init__(self, api_key: str, model_id: str = DEFAULT_DEEPGRAM_MODEL):
        """Initialize Deepgram ASR client.

        Args:
            api_key: Deepgram API key for authentication.
            model_id: Model to use for transcription.
        """
        self.api_key = api_key
        self.model_id = model_id
        self._requests_session: requests.Session = requests.Session()

    def _get_transcript_path(self, activity_id: str) -> Path:
        """Get the path for cached transcript file.

        Args:
            activity_id: Activity identifier.

        Returns:
            Path to transcript file.
        """
        transcript_dir = Path(__file__).parent.parent / TRANSCRIPT_DIR_NAME
        transcript_dir.mkdir(parents=True, exist_ok=True)
        return transcript_dir / f"{activity_id}_deepgram_transcript.json"

    def _load_cached_transcript(self, activity_id: str) -> dict | None:
        """Load cached transcript if available.

        Args:
            activity_id: Activity identifier.

        Returns:
            Cached transcript data or None if not available.
        """
        transcript_path = self._get_transcript_path(activity_id)
        if transcript_path.exists():
            logger.info(f"Activity {activity_id}: Loading cached transcript")
            with open(transcript_path) as f:
                return json.load(f)
        return None

    def _save_transcript(self, activity_id: str, transcript_json: dict) -> None:
        """Save transcript to cache file.

        Args:
            activity_id: Activity identifier.
            transcript_json: Transcript data to save.
        """
        transcript_path = self._get_transcript_path(activity_id)
        with open(transcript_path, "w") as f:
            json.dump(transcript_json, f)

    def _create_transcript_item(self, transcript_json: dict) -> TranscriptItem:
        """Create TranscriptItem from Deepgram response.

        Args:
            transcript_json: Deepgram transcript response.

        Returns:
            Structured transcript item.
        """
        return TranscriptItem(
            transcript=transcript_json["transcript"],
            words=[
                WordItem(
                    word=word["word"],
                    start=word["start"],
                    end=word["end"],
                    confidence=word["confidence"],
                )
                for word in transcript_json["words"]
            ],
        )

    async def transcribe_async(
        self, activity: Activity, save_transcript: bool = False
    ) -> TranscriptItem | None:
        """Transcribe audio data asynchronously using Deepgram API.

        Args:
            activity: Activity object containing audio data to transcribe.
            save_transcript: Whether to save transcript to disk.

        Returns:
            TranscriptItem with transcript and word-level timing, or None if failed.
        """
        logger.info(
            f"Activity {activity.activity_id}: Starting async transcription with Deepgram model {self.model_id}"
        )

        try:
            transcript_json = None
            if save_transcript:
                transcript_json = self._load_cached_transcript(activity.activity_id)

            if transcript_json is None:
                logger.info(
                    f"Activity {activity.activity_id}: Transcribing with Deepgram (memory-only: {not save_transcript})"
                )

                async with aiohttp.ClientSession() as session:
                    url = f"{DEEPGRAM_API_URL}?model={self.model_id}&{DEEPGRAM_PARAMS}"
                    headers = {
                        "Authorization": f"Token {self.api_key}",
                        "Content-Type": "audio/wav",
                    }

                    async with session.post(
                        url, headers=headers, data=activity.audio_file_data
                    ) as response:
                        response.raise_for_status()
                        response_data = await response.json()
                        transcript_json = response_data["results"]["channels"][0]["alternatives"][0]

                        if save_transcript:
                            self._save_transcript(activity.activity_id, transcript_json)

            word_count = len(transcript_json["words"])
            transcript_length = len(transcript_json["transcript"])

            logger.info(
                f"Activity {activity.activity_id}: Deepgram transcription completed - {word_count} words, {transcript_length} characters"
            )

            return self._create_transcript_item(transcript_json)

        except aiohttp.ClientError as e:
            logger.error(
                f"Activity {activity.activity_id}: Request error during transcription: {e}"
            )
        except KeyError as e:
            logger.error(
                f"Activity {activity.activity_id}: Unexpected response format from Deepgram: {e}"
            )
        except Exception as e:
            logger.error(
                f"Activity {activity.activity_id}: Unexpected error during transcription: {e}"
            )
            raise e
        return None

    def transcribe(
        self, activity: Activity, save_transcript: bool = False
    ) -> TranscriptItem | None:
        """Transcribe audio data using Deepgram API.

        Args:
            activity: Activity object containing audio data to transcribe.
            save_transcript: Whether to save transcript to disk.

        Returns:
            TranscriptItem with transcript and word-level timing, or None if failed.

        Raises:
            requests.exceptions.RequestException: If API request fails.
            KeyError: If response format is unexpected.
            Exception: For other transcription errors.
        """
        logger.info(
            f"Activity {activity.activity_id}: Starting transcription with Deepgram model {self.model_id}"
        )

        try:
            transcript_json = None
            if save_transcript:
                transcript_json = self._load_cached_transcript(activity.activity_id)

            if transcript_json is None:
                logger.info(
                    f"Activity {activity.activity_id}: Transcribing with Deepgram (memory-only: {not save_transcript})"
                )

                url = f"{DEEPGRAM_API_URL}?model={self.model_id}&{DEEPGRAM_PARAMS}"
                headers = {
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "audio/wav",
                }

                response = self._requests_session.post(
                    url, headers=headers, data=activity.audio_file_data
                )
                response.raise_for_status()
                response_data = response.json()
                transcript_json = response_data["results"]["channels"][0]["alternatives"][0]

                if save_transcript:
                    self._save_transcript(activity.activity_id, transcript_json)

            word_count = len(transcript_json["words"])
            transcript_length = len(transcript_json["transcript"])

            logger.info(
                f"Activity {activity.activity_id}: Deepgram transcription completed - {word_count} words, {transcript_length} characters"
            )

            return self._create_transcript_item(transcript_json)

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Activity {activity.activity_id}: Request error during transcription: {e}"
            )
        except KeyError as e:
            logger.error(
                f"Activity {activity.activity_id}: Unexpected response format from Deepgram: {e}"
            )
        except Exception as e:
            logger.error(
                f"Activity {activity.activity_id}: Unexpected error during transcription: {e}"
            )
            raise e
        return None

    def close(self) -> None:
        """Close underlying HTTP session(s)."""
        try:
            self._requests_session.close()
        except Exception:
            pass


class KaldiASRClient:
    """Client for Kaldi automatic speech recognition.

    Provides a clean interface for transcribing phrase audio data using Kaldi's API
    with automatic routing to the correct service based on story type.
    """

    def __init__(self) -> None:
        """Initialize Kaldi ASR client with hardcoded URLs and graph ID."""
        self.general_client = KaldiClient(url=GENERAL_KALDI_URL)
        self.partner_client = KaldiClient(url=PARTNER_KALDI_URL)

    def _get_asr_client(self, activity: Activity) -> KaldiClient:
        """Get the appropriate ASR client based on activity story type.

        Args:
            activity: Activity object containing story type information.

        Returns:
            Appropriate KaldiClient instance.

        Raises:
            ValueError: If no valid client exists for the activity.
        """
        if activity.is_kaldi_general:
            return self.general_client
        elif activity.is_kaldi_partner:
            return self.partner_client
        else:
            raise ValueError(
                f"No valid kaldi client for activity {activity.activity_id}, story_tags={activity.story_tags}"
            )

    def _calculate_audio_duration(
        self, audio_data: bytes, activity_id: str, phrase_index: int
    ) -> None:
        """Calculate and log audio duration for debugging purposes.

        Args:
            audio_data: Raw audio bytes.
            activity_id: Activity identifier for logging.
            phrase_index: Phrase index for logging.
        """
        try:
            audio_buffer = io.BytesIO(audio_data)
            audio, sr = sf.read(audio_buffer)
            duration_s = len(audio) / sr
            logger.info(
                f"Activity {activity_id}: Transcribing phrase {phrase_index} with Kaldi client duration {duration_s:.2f}s"
            )
        except Exception:
            logger.info(
                f"Activity {activity_id}: Transcribing phrase {phrase_index} with Kaldi client"
            )

    def _create_transcript_item(self, kaldi_data: dict) -> TranscriptItem:
        """Create TranscriptItem from Kaldi response.

        Args:
            kaldi_data: Kaldi transcription response data.

        Returns:
            Structured transcript item.
        """
        words = [
            WordItem(
                word=word_data["word"],
                start=word_data["start_time"],
                end=word_data["end_time"],
                confidence=word_data["confidence"],
            )
            for word_data in kaldi_data["transcription"]
        ]
        return TranscriptItem(transcript=kaldi_data["text"], words=words)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def transcribe(
        self, activity: Activity, phrase_index: int, audio_data: bytes
    ) -> TranscriptItem | None:
        """Transcribe phrase audio data using Kaldi API.

        Args:
            activity: Activity object for story type routing and phrase ID generation.
            phrase_index: Phrase index for logging.
            audio_data: Raw audio bytes (WAV format) for the phrase.

        Returns:
            TranscriptItem with transcript and word-level timing, or None if failed.
        """
        client = self._get_asr_client(activity)
        lm_phrase_id = f"{activity.story_id}_{phrase_index + 1}"

        logger.info(
            f"Activity {activity.activity_id}: Transcribing phrase {phrase_index} with Kaldi client {client._url}"
        )

        self._calculate_audio_duration(audio_data, activity.activity_id, phrase_index)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, buffering=0) as tmp_file:
            tmp_file.write(audio_data)

        try:
            metadata: dict[str, dict[str, Any]] = {"asr": {}}
            loop = asyncio.get_event_loop()
            from functools import partial

            fetch_fn = partial(
                client.fetch_kaldi_data,
                kaldi_type="kaldi",
                graph_id=KALDI_GRAPH_ID,
                lm_phrase_id=lm_phrase_id,
                metadata=metadata,
                audio_path=tmp_file.name,
            )
            result = await loop.run_in_executor(None, fetch_fn)

            kaldi_data = result["asr"]["kaldi"]["data"]
            transcript_item = self._create_transcript_item(kaldi_data)

            logger.info(
                f"Activity {activity.activity_id}: Kaldi transcription completed for phrase {phrase_index} - "
                f"{len(transcript_item.words)} words, {len(transcript_item.transcript)} characters"
            )

            return transcript_item

        finally:
            try:
                os.unlink(tmp_file.name)
            except OSError:
                pass

    def close(self) -> None:
        """Clean up both Kaldi client resources."""
        self.general_client.close()
        self.partner_client.close()


class W2VASRClient:
    """Client for Wav2Vec automatic speech recognition.

    Provides a clean interface for transcribing phrase audio data using Wav2Vec's API
    with in-memory audio processing.
    """

    def __init__(self) -> None:
        """Initialize W2V ASR client with hardcoded endpoint."""
        self.w2v_client = W2VStackClient(W2V_ENDPOINT)
        logger.info(f"W2VASRClient initialized with endpoint={W2V_ENDPOINT}")

    def _resample_audio(self, audio: np.ndarray, original_sr: int) -> np.ndarray:
        """Resample audio to target sample rate if needed.

        Args:
            audio: Audio data as numpy array.
            original_sr: Original sample rate.

        Returns:
            Resampled audio data.
        """
        if original_sr != W2V_TARGET_SAMPLE_RATE:
            target_length = int(len(audio) * W2V_TARGET_SAMPLE_RATE / original_sr)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, target_length), np.arange(len(audio)), audio
            ).astype(np.float32)
        return audio

    def _truncate_audio_if_needed(
        self, audio: np.ndarray, activity_id: str, phrase_index: int
    ) -> np.ndarray:
        """Truncate audio if it exceeds maximum length for W2V endpoint.

        Args:
            audio: Audio data as numpy array.
            activity_id: Activity identifier for logging.
            phrase_index: Phrase index for logging.

        Returns:
            Truncated audio data if necessary.
        """
        max_samples = int((W2V_MAX_PHRASE_LENGTH_MS / 1000.0) * W2V_TARGET_SAMPLE_RATE)
        if len(audio) > max_samples:
            original_duration_ms = len(audio) / W2V_TARGET_SAMPLE_RATE * 1000
            audio = audio[:max_samples]
            logger.warning(
                f"Activity {activity_id}: Truncating phrase {phrase_index} audio from {original_duration_ms:.0f}ms to {W2V_MAX_PHRASE_LENGTH_MS}ms"
            )
        return audio

    def _load_audio_data(
        self, audio_data: bytes, activity_id: str, phrase_index: int
    ) -> np.ndarray:
        """Convert audio bytes to numpy array for W2V processing.

        Args:
            audio_data: Raw audio bytes (WAV format).
            activity_id: Activity identifier for logging.
            phrase_index: Phrase index for logging.

        Returns:
            Numpy array at target sample rate.
        """
        audio_buffer = io.BytesIO(audio_data)
        audio, original_sr = sf.read(audio_buffer)

        duration_ms = len(audio) / original_sr * 1_000
        logger.info(
            f"Activity {activity_id}: Loading phrase {phrase_index} audio with W2V client duration {duration_ms:.1f}ms"
        )

        audio = self._resample_audio(audio, original_sr)
        audio = self._truncate_audio_if_needed(audio, activity_id, phrase_index)

        return audio

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def transcribe(
        self, activity: Activity, phrase_index: int, audio_data: bytes
    ) -> TranscriptItem | None:
        """Transcribe phrase audio data using W2V API.

        Args:
            activity: Activity object for logging.
            phrase_index: Phrase index for logging.
            audio_data: Raw audio bytes (WAV format) for the phrase.

        Returns:
            TranscriptItem with transcript and word-level timing, or None if failed.
        """
        logger.info(f"Activity {activity.activity_id}: Transcribing phrase {phrase_index} with W2V")

        audio = self._load_audio_data(audio_data, activity.activity_id, phrase_index)

        w2v_response = self.w2v_client.transcribe(
            audio, incremental=True, streaming=False, as_pm=False
        )

        if "transcription" not in w2v_response:
            logger.warning(
                f"Activity {activity.activity_id}: W2V response missing 'transcription' field"
            )
            return None

        transcription_data = w2v_response["transcription"]

        logger.info(
            f"Activity {activity.activity_id}: W2V transcription completed for phrase {phrase_index} - {len(transcription_data)} characters"
        )

        return TranscriptItem(transcript=transcription_data, words=[])

    def close(self) -> None:
        """Close underlying client if supported."""
        try:
            close_fn = getattr(self.w2v_client, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception:
            pass
