import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlencode

import aiohttp
import numpy as np
import orjson as json
from aiohttp import ClientSession, ClientTimeout, WSMsgType
from amira_pyutils.shared.core.errors import AmiraError
from amira_pyutils.shared.core.logging import get_logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class W2VError(AmiraError):
    """Error for W2V client failures."""


logger = get_logger(__name__)

DEFAULT_SESSION_TIMEOUT: Final[float] = 30.0
DEFAULT_REQUEST_TIMEOUT: Final[float] = 30.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_FACTOR: Final[float] = 0.5
DEFAULT_CHUNK_SIZE: Final[int] = 6400
DEFAULT_TRANSCRIPTION_CHUNK_SIZE: Final[int] = 256_000
RIFF_HEADER_PREFIX: Final[bytes] = b"RIFF"
SAMPLE_RATE: Final[int] = 16_000
BYTES_PER_SAMPLE: Final[int] = 4
END_OF_INPUT_MARKER: Final[bytes] = b"\0"


class TranscriptionMode(StrEnum):
    """Transcription processing modes."""

    STREAMING = "decode_stream_sock"
    NON_REALTIME = "decode_nrt_sock"
    BATCH = "decode"


class ChunkFormat(StrEnum):
    """Audio chunk formats."""

    RAW = "raw"
    WAV = "wav"


class RetryableStatus(StrEnum):
    """HTTP status codes that should trigger retries."""

    INTERNAL_SERVER_ERROR = "500"
    BAD_GATEWAY = "502"
    SERVICE_UNAVAILABLE = "503"
    GATEWAY_TIMEOUT = "504"


@dataclass(frozen=True)
class W2VConfig:
    """Configuration for Wave2Vec2 client."""

    url: str
    model: str | None = None
    session_timeout: float = DEFAULT_SESSION_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.url.startswith("https://"):
            raise ValueError("URL must include protocol (https://)")


@dataclass(frozen=True)
class StreamingParams:
    """Parameters for streaming transcription."""

    activity_id: str | None = None
    phrase_index: str | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    inter_chunk_delay: float = 0.0
    as_pm: bool = True


@dataclass(frozen=True)
class TranscriptionResult:
    """Result from transcription operation."""

    status: str
    data: dict[str, Any]
    trip_latency: float
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class BatchTranscriptionRequest:
    """Request for batch transcription."""

    audio_buffer: list[int]
    description: str = "W2V transcription request"
    opaque: dict[str, Any] | None = None
    incremental: bool = True


class W2VClient:
    """Async Wave2Vec2 client for speech transcription.

    Note: Authentication mode is not currently implemented. If auth is required,
    headers should be passed to ws_connect and session requests.
    """

    def __init__(self, *, config: W2VConfig) -> None:
        """Initialize the W2V client.

        Args:
            config: Configuration for the client
        """
        self._config = config
        self._session: ClientSession | None = None
        self._streaming_connection_count = 0

    async def __aenter__(self) -> "W2VClient":
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

    def _build_endpoint(
        self,
        *,
        mode: TranscriptionMode,
        params: dict[str, str] | None = None,
    ) -> str:
        """Build endpoint URL with optional parameters.

        Args:
            mode: Transcription mode
            params: Query parameters

        Returns:
            Complete endpoint URL
        """
        endpoint = f"/{mode.value}"

        if self._config.model:
            endpoint += f"/{self._config.model}"

        if params:
            query_string = urlencode(params)
            endpoint += f"?{query_string}"

        return endpoint

    def _prepare_audio_chunk(
        self,
        *,
        chunk: bytes,
        is_numpy_array: bool,
    ) -> bytes:
        """Prepare audio chunk for transmission.

        Args:
            chunk: Raw audio chunk
            is_numpy_array: Whether source was numpy array

        Returns:
            Prepared audio chunk

        Note:
            For numpy arrays, we send raw data. For bytes that don't have
            RIFF header, we assume raw format and send as-is since constructing
            a proper WAV header requires additional audio parameters.
        """
        if is_numpy_array:
            return chunk

        if not chunk.startswith(RIFF_HEADER_PREFIX):
            return chunk

        return chunk

    async def _stream_audio_chunks(
        self,
        *,
        audio_bytes: bytes,
        chunk_size: int,
        inter_chunk_delay: float,
        timings: list[tuple[int, float]],
    ) -> AsyncGenerator[bytes, None]:
        """Stream audio chunks with timing information.

        Args:
            audio_bytes: Complete audio data
            chunk_size: Size of each chunk
            inter_chunk_delay: Delay between chunks
            timings: List to populate with (offset, timestamp) tuples

        Yields:
            Audio chunks
        """
        offset = 0

        while offset < len(audio_bytes):
            chunk = audio_bytes[offset : offset + chunk_size]
            offset += chunk_size
            timings.append((offset, time.time()))
            yield chunk

            if inter_chunk_delay > 0:
                await asyncio.sleep(inter_chunk_delay)

    async def _handle_websocket_message(
        self,
        *,
        msg: aiohttp.WSMessage,
        handler: Callable[[dict[str, Any]], None],
        timings: list[tuple[int, float]],
        received_idx: int,
    ) -> int:
        """Handle incoming websocket message.

        Args:
            msg: Websocket message
            handler: Result handler function
            timings: Timing information
            received_idx: Current received index

        Returns:
            Updated received index

        Raises:
            W2VError: If service returns error
            ValueError: If unexpected message type received
        """
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return received_idx

        if msg.type != WSMsgType.TEXT:
            raise ValueError(f"Unexpected socket message type: {msg.type}")

        result = json.loads(msg.data)

        if result.get("status") == "error":
            raise W2VError(
                msg=f"Service error: {result.get('message', 'Unknown error')}",
                retryable=False,
            )

        result_offset = result.get("metadata", {}).get("audioOffset", 0)
        latency = self._calculate_latency(
            result_offset=result_offset,
            timings=timings,
            received_idx=received_idx,
        )

        result["tripLatency"] = latency
        handler(result)

        return self._update_received_index(
            result_offset=result_offset,
            timings=timings,
            received_idx=received_idx,
        )

    def _calculate_latency(
        self,
        *,
        result_offset: int,
        timings: list[tuple[int, float]],
        received_idx: int,
    ) -> float:
        """Calculate latency for current result.

        Args:
            result_offset: Audio offset from result (assumed to be in samples)
            timings: Timing information
            received_idx: Current received index

        Returns:
            Calculated latency
        """
        target_bytes: int = result_offset * BYTES_PER_SAMPLE

        latency: float = 0.0
        for i in range(received_idx, len(timings)):
            offset, ts = timings[i]
            if offset <= target_bytes:
                latency = time.time() - ts
            else:
                break
        return latency

    def _update_received_index(
        self,
        *,
        result_offset: int,
        timings: list[tuple[int, float]],
        received_idx: int,
    ) -> int:
        """Update received index based on result offset.

        Args:
            result_offset: Audio offset from result (assumed to be in samples)
            timings: Timing information
            received_idx: Current received index

        Returns:
            Updated received index
        """
        target_bytes: int = result_offset * BYTES_PER_SAMPLE

        while received_idx < len(timings) and timings[received_idx][0] <= target_bytes:
            received_idx += 1

        return received_idx

    async def transcribe_streaming(
        self,
        *,
        audio: np.ndarray | bytes,
        handler: Callable[[dict[str, Any]], None],
        params: StreamingParams | None = None,
    ) -> None:
        """Perform streaming transcription.

        Args:
            audio: Audio data as numpy array or bytes
            handler: Function to handle transcription results
            params: Streaming parameters

        Raises:
            W2VError: If transcription fails
            ConnectionResetError: If connection is reset
        """
        await self._ensure_session()

        if params is None:
            params = StreamingParams()

        audio_bytes = audio.tobytes() if isinstance(audio, np.ndarray) else audio
        is_numpy_array = isinstance(audio, np.ndarray)

        query_params: dict[str, str] = {}
        if params.activity_id:
            query_params["activityId"] = params.activity_id
        if params.phrase_index:
            query_params["phraseIndex"] = params.phrase_index
        if is_numpy_array:
            query_params["chunk_format"] = ChunkFormat.RAW.value

        mode = TranscriptionMode.STREAMING if params.as_pm else TranscriptionMode.NON_REALTIME
        endpoint = self._build_endpoint(mode=mode, params=query_params)

        timings: list[tuple[int, float]] = []
        received_idx = 0

        if self._session is None:
            raise W2VError(msg="Session not initialized", retryable=False)
        async with self._session.ws_connect(f"{self._config.url}{endpoint}") as ws:
            self._streaming_connection_count += 1
            logger.info(
                f"Established streaming W2V connection "
                f"({self._streaming_connection_count} concurrent)"
            )

            try:

                async def stream_audio_up() -> None:
                    async for chunk in self._stream_audio_chunks(
                        audio_bytes=audio_bytes,
                        chunk_size=params.chunk_size,
                        inter_chunk_delay=params.inter_chunk_delay,
                        timings=timings,
                    ):
                        prepared_chunk = self._prepare_audio_chunk(
                            chunk=chunk,
                            is_numpy_array=is_numpy_array,
                        )
                        await ws.send_bytes(prepared_chunk)

                    await ws.send_bytes(END_OF_INPUT_MARKER)

                async def stream_transcriptions_down() -> None:
                    nonlocal received_idx
                    while True:
                        msg = await ws.receive()

                        if msg.type in (
                            WSMsgType.CLOSE,
                            WSMsgType.CLOSING,
                            WSMsgType.CLOSED,
                        ):
                            break

                        received_idx = await self._handle_websocket_message(
                            msg=msg,
                            handler=handler,
                            timings=timings,
                            received_idx=received_idx,
                        )

                await asyncio.gather(stream_audio_up(), stream_transcriptions_down())

            except ConnectionResetError:
                self._handle_connection_reset(ws)
                raise
            finally:
                self._streaming_connection_count -= 1

    def _handle_connection_reset(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Handle connection reset scenarios.

        Args:
            ws: Websocket connection

        Raises:
            ConnectionResetError: With appropriate error message
        """
        if not ws.closed:
            return

        if ws.close_code == aiohttp.WSCloseCode.TRY_AGAIN_LATER:
            raise ConnectionResetError("Server refused session due to load")
        elif ws.close_code == aiohttp.WSCloseCode.ABNORMAL_CLOSURE:
            raise ConnectionResetError("Internal server error handling session")

    @retry(
        stop=stop_after_attempt(DEFAULT_MAX_RETRIES),
        wait=wait_exponential(multiplier=DEFAULT_BACKOFF_FACTOR),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def transcribe_batch(
        self,
        *,
        audio: np.ndarray | bytes,
        incremental: bool = True,
        opaque: dict[str, Any] | None = None,
    ) -> TranscriptionResult:
        """Perform batch transcription.

        Args:
            audio: Audio data as numpy array or bytes
            incremental: Whether to use incremental processing
            opaque: Opaque data to include in request

        Returns:
            Transcription result

        Raises:
            W2VError: If transcription fails
        """
        await self._ensure_session()

        if isinstance(audio, np.ndarray):
            audio_buffer = audio.tolist()
        else:
            audio_buffer = list(audio)

        request = BatchTranscriptionRequest(
            audio_buffer=audio_buffer,
            opaque=opaque,
            incremental=incremental,
        )

        endpoint = self._build_endpoint(mode=TranscriptionMode.BATCH)
        start_time = time.time()

        try:
            if self._session is None:
                raise W2VError(msg="Session not initialized", retryable=False)
            async with self._session.post(
                f"{self._config.url}{endpoint}",
                json=request.__dict__,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                result_data = await response.json()

                if "error" in result_data:
                    raise W2VError(
                        msg=f"{result_data['error']}: {result_data.get('message', 'Unknown error')}",
                        retryable=False,
                    )

                trip_latency = time.time() - start_time

                return TranscriptionResult(
                    status=result_data.get("status", "unknown"),
                    data=result_data,
                    trip_latency=trip_latency,
                    metadata=result_data.get("metadata"),
                )

        except aiohttp.ClientError as e:
            logger.error(f"Error transcribing audio: {e}")
            raise W2VError(msg=f"Transcription failed: {e}", retryable=False) from e
