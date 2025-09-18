import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import aiohttp
import orjson as json
from aiohttp import ClientSession, ClientTimeout

# TODO(amira_pyutils): swap to shared error and logger when available
if TYPE_CHECKING:  # pragma: no cover - types only
    pass


class AmiraError(Exception):
    def __init__(self, msg: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(msg if msg is not None else "")
        self.retryable = retryable


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class KaldiError(AmiraError):
    """Error for Kaldi transcription failures."""


logger = get_logger(__name__)

DEFAULT_SESSION_TIMEOUT: Final[float] = 30.0
DEFAULT_REQUEST_TIMEOUT: Final[float] = 30.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_FACTOR: Final[float] = 0.5


class KaldiResponseType(StrEnum):
    """Kaldi response types."""

    PLACEMENT = "placement"
    ASSESSMENT = "assessment"


class RetryableStatus(StrEnum):
    """HTTP status codes that should trigger retries."""

    INTERNAL_SERVER_ERROR = "500"
    BAD_GATEWAY = "502"
    SERVICE_UNAVAILABLE = "503"
    GATEWAY_TIMEOUT = "504"


@dataclass(frozen=True)
class KaldiConfig:
    """Configuration for Kaldi client."""

    url: str
    session_timeout: float = DEFAULT_SESSION_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.url.startswith("https://"):
            raise ValueError("URL must include protocol (https://)")


@dataclass(frozen=True)
class WordTranscription:
    """Word-level transcription with timing and confidence."""

    word: str
    confidence: float
    start_time: float
    end_time: float


@dataclass(frozen=True)
class TranscriptionData:
    """Structured transcription data."""

    text: str
    transcription: list[WordTranscription]
    raw: dict[str, Any]


@dataclass(frozen=True)
class KaldiResult:
    """Result from Kaldi transcription."""

    data: TranscriptionData
    graph_id: str
    kaldi_type: str


class KaldiClient:
    """Async Kaldi client for speech transcription."""

    def __init__(self, *, config: KaldiConfig) -> None:
        """Initialize the Kaldi client.

        Args:
            config: Configuration for the client
        """
        self._config = config
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "KaldiClient":
        """Async context manager entry."""
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def _ensure_session(self) -> None:
        """Ensure HTTP session is initialized."""
        if self._session is None:
            timeout = ClientTimeout(
                total=self._config.session_timeout,
                connect=self._config.request_timeout,
            )
            self._session = ClientSession(timeout=timeout)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    @retry(  # type: ignore[misc]
        stop=stop_after_attempt(DEFAULT_MAX_RETRIES),
        wait=wait_exponential(multiplier=DEFAULT_BACKOFF_FACTOR),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def transcribe_audio(
        self,
        *,
        audio_path: Path,
        graph_id: str,
        lm_phrase_id: str,
        kaldi_type: str,
    ) -> KaldiResult:
        """Transcribe audio file using Kaldi service.

        Args:
            audio_path: Path to audio file
            graph_id: Graph identifier for Kaldi
            lm_phrase_id: Language model phrase identifier
            kaldi_type: Type of Kaldi processing

        Returns:
            Structured transcription result

        Raises:
            KaldiError: If transcription fails
        """
        await self._ensure_session()

        try:
            audio_data = await self._read_audio_file(audio_path=audio_path)
            response = await self._make_request(
                graph_id=graph_id,
                lm_phrase_id=lm_phrase_id,
                audio_data=audio_data,
            )

            raw_data = self._parse_response(response_text=response)
            transcription_data = self._extract_transcription_data(raw_data=raw_data)

            return KaldiResult(
                data=transcription_data,
                graph_id=graph_id,
                kaldi_type=kaldi_type,
            )

        except Exception as e:
            logger.error(f"Kaldi transcription failed: {e!s}")
            raise KaldiError(msg=f"Transcription failed: {e}", retryable=False) from e

    async def _read_audio_file(self, *, audio_path: Path) -> bytes:
        """Read audio file asynchronously.

        Args:
            audio_path: Path to audio file

        Returns:
            Audio file content as bytes
        """
        return audio_path.read_bytes()

    async def _make_request(
        self,
        *,
        graph_id: str,
        lm_phrase_id: str,
        audio_data: bytes,
    ) -> str:
        """Make HTTP request to Kaldi service.

        Args:
            graph_id: Graph identifier
            lm_phrase_id: Language model phrase identifier
            audio_data: Audio file content

        Returns:
            Response text from Kaldi service
        """
        url: str = f"{self._config.url}/{graph_id}?phraseid={lm_phrase_id}"

        if self._session is None:
            raise KaldiError(msg="Session not initialized", retryable=False)
        async with self._session.post(url, data=audio_data) as response:
            response.raise_for_status()
            text_any = await response.text()
            from typing import cast

            return cast(str, text_any)

    def _parse_response(self, *, response_text: str) -> dict[str, Any]:
        """Parse Kaldi response based on format.

        Args:
            response_text: Raw response text from Kaldi

        Returns:
            Parsed response data

        Raises:
            KaldiError: If response parsing fails
        """
        try:
            lines: list[str] = response_text.strip().split("\n")

            if len(lines) > 2:
                result = json.loads(lines[-2])
                from typing import cast

                return cast(dict[str, Any], result)
            else:
                result = json.loads(response_text)
                from typing import cast

                return cast(dict[str, Any], result)

        except (json.JSONDecodeError, IndexError) as e:
            raise KaldiError(msg=f"Failed to parse Kaldi response: {e}", retryable=False) from e

    def _extract_transcription_data(self, *, raw_data: dict[str, Any]) -> TranscriptionData:
        """Extract structured transcription data from raw response.

        Args:
            raw_data: Raw parsed response data

        Returns:
            Structured transcription data

        Raises:
            KaldiError: If data extraction fails
        """
        try:
            text_blocks = raw_data["data"][0]["text"]
            # TODO can we read this directly into the WordTranscription class?
            transcription = [
                WordTranscription(
                    word=block["value"],
                    confidence=block["confidence"],
                    start_time=block["start_time"],
                    end_time=block["end_time"],
                )
                for block in text_blocks
            ]

            text = " ".join(word.word for word in transcription)

            return TranscriptionData(
                text=text,
                transcription=transcription,
                raw=raw_data,
            )

        except (KeyError, IndexError, TypeError) as e:
            raise KaldiError(
                msg=f"Failed to extract transcription data: {e}", retryable=False
            ) from e
