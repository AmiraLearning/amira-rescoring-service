import os
import time
import traceback

import numpy as np

from amira_pyutils.logging import get_logger
from src.letter_scoring_pipeline.exceptions import (
    DecodingError,
    ModelNotReadyError,
    TritonConnectionError,
)

try:
    import tritonclient.http as httpclient
    from tritonclient.utils import InferenceServerException

    TRITON_AVAILABLE = True
except ImportError:
    TRITON_AVAILABLE = False

from amira_pyutils.logging import emit_emf_metric

from .constants import MS_PER_SECOND, DeviceType
from .decoder import PhonemeDecoder
from .metrics_constants import (
    DIM_CORRELATION_ID,
    DIM_DEVICE,
    MET_INFER_DECODE_MS,
    MET_INFER_INCLUDE_CONF,
    MET_INFER_MODEL_MS,
    MET_INFER_PRE_MS,
    MET_INFER_SUCCESS,
    MET_INFER_TOTAL_MS,
    NS_INFERENCE,
)
from .models import (
    GPUInferenceResult,
    InferenceInput,
    PhoneticTranscript,
    W2VConfig,
)

try:
    from transformers import Wav2Vec2Processor as _Wav2Vec2Processor
except Exception:
    _Wav2Vec2Processor = None


logger = get_logger(__name__)

if not TRITON_AVAILABLE:
    logger.warning("Triton client not available. Install with: pip install tritonclient[http]")


class TritonInferenceEngine:
    """Triton inference engine for remote w2v inference."""

    _processor: "_Wav2Vec2Processor | None" = None

    def __init__(self, *, w2v_config: W2VConfig) -> None:
        if not TRITON_AVAILABLE:
            raise ImportError(
                "Triton client not available. Install with: pip install tritonclient[http]"
            )

        self._w2v_config = w2v_config
        self._decoder = PhonemeDecoder()
        self._processor = None
        try:
            from transformers import Wav2Vec2Processor

            self._processor = Wav2Vec2Processor.from_pretrained(w2v_config.model_path)
            logger.info("Loaded W2V2 processor for Triton decoding")
        except ImportError as e:
            logger.warning(f"Failed to load W2V2 processor for decoding (continuing without): {e}")
        except Exception as e:
            logger.warning(f"Unexpected error loading W2V2 processor: {type(e).__name__}: {e}")

        try:
            raw_url = w2v_config.triton_url
            if not isinstance(raw_url, str) or not raw_url.strip():
                raise ValueError("Triton URL must be a non-empty https URL")
            url_lower = raw_url.lower().strip()
            if url_lower.startswith("http://"):
                raise ValueError("Insecure Triton URL (http://) is not allowed. Use https://")
            if not url_lower.startswith("https://"):
                raise ValueError("Triton URL must start with https://")

            host = raw_url.replace("https://", "", 1)
            # Reuse a single HTTP(s) connection with timeouts
            self._client = httpclient.InferenceServerClient(
                url=host,
                verbose=False,
                ssl=True,
                concurrency=1,
                network_timeout=10.0,
                connection_timeout=5.0,
                network_retry=2,
            )

            if not self._client.is_server_ready():
                raise TritonConnectionError(f"Triton server not ready at {w2v_config.triton_url}")

            if not self._client.is_model_ready(w2v_config.triton_model):
                raise ModelNotReadyError(f"Triton model '{w2v_config.triton_model}' not ready")

            logger.info(f"Connected to Triton server at {w2v_config.triton_url}")

        except (ValueError, TritonConnectionError, ModelNotReadyError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error connecting to Triton server: {type(e).__name__}: {e}")
            raise TritonConnectionError(f"Failed to connect to Triton server: {e}") from e

    def infer(self, *, input_data: InferenceInput) -> GPUInferenceResult:
        """Perform inference using Triton server.

        Args:
            input_data: The input data containing audio array.

        Returns:
            GPUInferenceResult: The inference result.
        """
        result = GPUInferenceResult(
            inference_id=input_data.inference_id,
            device=DeviceType.GPU,
        )

        try:
            inference_start = time.time()

            preprocess_start = time.time()

            # Size validation - prevent memory issues with extremely large arrays
            max_audio_samples = int(
                os.getenv("MAX_AUDIO_SAMPLES", "16000000")
            )  # ~1000 seconds at 16kHz
            if input_data.audio_array.size > max_audio_samples:
                raise ValueError(
                    f"Audio array too large: {input_data.audio_array.size} samples "
                    f"(max allowed: {max_audio_samples}). This could cause memory issues."
                )

            logger.debug(f"Processing audio array: {input_data.audio_array.size} samples")
            audio_input: np.ndarray = input_data.audio_array.astype(np.float32)
            if audio_input.ndim == 1:
                audio_input = audio_input[np.newaxis, :]

            result.preprocess_time_ms = (time.time() - preprocess_start) * MS_PER_SECOND

            triton_input = httpclient.InferInput("INPUT__0", audio_input.shape, "FP32")
            triton_input.set_data_from_numpy(audio_input)

            triton_output = httpclient.InferRequestedOutput("OUTPUT__0")

            model_start = time.time()
            response = self._client.infer(
                model_name=self._w2v_config.triton_model,
                inputs=[triton_input],
                outputs=[triton_output],
            )
            result.model_inference_time_ms = (time.time() - model_start) * MS_PER_SECOND

            logits = response.as_numpy("OUTPUT__0")

            decode_start = time.time()
            predicted_ids = np.argmax(logits, axis=-1)

            if self._processor is not None:
                try:
                    try:
                        import torch

                        ids_tensor = torch.tensor(predicted_ids, dtype=torch.long)
                        if self._processor is None:
                            raise ValueError("Processor not initialized")
                        transcription = self._processor.batch_decode(ids_tensor)[0]
                        result.transcription = transcription
                        ids_list = predicted_ids[0].tolist()
                        if hasattr(self._processor, "tokenizer") and self._processor.tokenizer:
                            result.pred_tokens = self._processor.tokenizer.convert_ids_to_tokens(
                                ids_list
                            )
                    except Exception as e:  # pragma: no cover - optional decoding path
                        logger.warning(f"Decoding with processor failed: {e}")
                        result.transcription = ""
                        result.pred_tokens = []
                except (IndexError, KeyError, ValueError) as e:
                    logger.debug(f"Token conversion failed: {type(e).__name__}: {e}")
                    result.transcription = ""
                    result.pred_tokens = []
            else:
                result.transcription = ""
                result.pred_tokens = []

            result.decode_time_ms = (time.time() - decode_start) * MS_PER_SECOND

            if self._w2v_config.include_confidence:
                conf_start = time.time()
                exp_logits = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
                probs = exp_logits / np.sum(exp_logits, axis=-1, keepdims=True)
                max_probs = np.max(probs, axis=-1)[0]
                result.max_probs = max_probs
                result.confidence_calculation_time_ms = (time.time() - conf_start) * MS_PER_SECOND

            try:
                result.phonetic_transcript = self._decoder.decode(
                    pred_tokens=result.pred_tokens,
                    max_probs=result.max_probs if self._w2v_config.include_confidence else None,
                )
            except (DecodingError, ValueError, KeyError) as e:
                logger.warning(f"Phoneme decoding failed: {type(e).__name__}: {e}")
                result.phonetic_transcript = PhoneticTranscript()
            except Exception as e:
                logger.error(f"Unexpected phoneme decoding error: {type(e).__name__}: {e}")
                result.phonetic_transcript = PhoneticTranscript()

            result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
            result.success = True
            try:
                emit_emf_metric(
                    namespace=NS_INFERENCE,
                    metrics={
                        MET_INFER_TOTAL_MS: result.total_duration_ms,
                        MET_INFER_PRE_MS: result.preprocess_time_ms or 0.0,
                        MET_INFER_MODEL_MS: result.model_inference_time_ms or 0.0,
                        MET_INFER_DECODE_MS: result.decode_time_ms or 0.0,
                        MET_INFER_INCLUDE_CONF: 1.0 if self._w2v_config.include_confidence else 0.0,
                        MET_INFER_SUCCESS: 1.0,
                    },
                    dimensions={
                        DIM_DEVICE: result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device),
                        **(
                            {DIM_CORRELATION_ID: str(input_data.inference_id)}
                            if getattr(input_data, "inference_id", None)
                            else {}
                        ),
                    },
                )
            except Exception as e:
                logger.debug(f"Metrics emission failed (non-fatal): {type(e).__name__}: {e}")

        except InferenceServerException as e:
            logger.error(f"Triton inference error: {e}")
            result.success = False
            result.error = f"Triton inference error: {e}"
            try:
                emit_emf_metric(
                    namespace=NS_INFERENCE,
                    metrics={
                        MET_INFER_SUCCESS: 0.0,
                    },
                    dimensions={
                        DIM_DEVICE: result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device),
                        **(
                            {DIM_CORRELATION_ID: str(input_data.inference_id)}
                            if getattr(input_data, "inference_id", None)
                            else {}
                        ),
                    },
                )
            except Exception as e:
                logger.debug(f"Metrics emission failed (non-fatal): {type(e).__name__}: {e}")
        except (ValueError, TypeError, RuntimeError) as e:
            logger.error(f"Inference error: {type(e).__name__}: {e}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            result.success = False
            result.error = str(e)
        except Exception as e:
            logger.error(f"Unexpected error during Triton inference: {type(e).__name__}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            result.success = False
            result.error = str(e)
            try:
                emit_emf_metric(
                    namespace=NS_INFERENCE,
                    metrics={
                        MET_INFER_SUCCESS: 0.0,
                    },
                    dimensions={
                        DIM_DEVICE: result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device),
                        **(
                            {DIM_CORRELATION_ID: str(input_data.inference_id)}
                            if getattr(input_data, "inference_id", None)
                            else {}
                        ),
                    },
                )
            except Exception as e:
                logger.debug(f"Metrics emission failed (non-fatal): {type(e).__name__}: {e}")

        return result


def create_inference_engine(*, w2v_config: W2VConfig) -> TritonInferenceEngine:
    """Create a Triton inference engine.

    Args:
        w2v_config: W2V configuration with Triton settings.

    Returns:
        TritonInferenceEngine: The inference engine.
    """
    return TritonInferenceEngine(w2v_config=w2v_config)
