import time
import traceback

import numpy as np
from loguru import logger

try:
    import tritonclient.http as httpclient
    from tritonclient.utils import InferenceServerException

    TRITON_AVAILABLE = True
except ImportError:
    TRITON_AVAILABLE = False
    logger.warning("Triton client not available. Install with: pip install tritonclient[http]")

from utils.logging import emit_emf_metric

from .constants import MS_PER_SECOND, DeviceType
from .decoder import PhonemeDecoder
from .models import (
    GPUInferenceResult,
    InferenceInput,
    PhoneticTranscript,
    W2VConfig,
)


class TritonInferenceEngine:
    """Triton inference engine for remote w2v inference."""

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
        except Exception as e:
            logger.warning(f"Failed to load W2V2 processor for decoding (continuing without): {e}")

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
            self._client = httpclient.InferenceServerClient(
                url=host,
                verbose=False,
                ssl=True,
            )

            if not self._client.is_server_ready():
                raise ConnectionError(f"Triton server not ready at {w2v_config.triton_url}")

            if not self._client.is_model_ready(w2v_config.triton_model):
                raise ConnectionError(f"Triton model '{w2v_config.triton_model}' not ready")

            logger.info(f"Connected to Triton server at {w2v_config.triton_url}")

        except Exception as e:
            logger.error(f"Failed to connect to Triton server: {e}")
            raise

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
            audio_input = input_data.audio_array.astype(np.float32)
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
                except Exception:
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
            except Exception as e:
                logger.warning(f"Phoneme decoding failed: {e}")
                result.phonetic_transcript = PhoneticTranscript()

            result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
            result.success = True
            try:
                emit_emf_metric(
                    namespace="Amira/Inference",
                    metrics={
                        "InferenceTotalMs": result.total_duration_ms,
                        "PreprocessMs": result.preprocess_time_ms or 0.0,
                        "ModelMs": result.model_inference_time_ms or 0.0,
                        "DecodeMs": result.decode_time_ms or 0.0,
                        "IncludeConfidence": 1.0 if self._w2v_config.include_confidence else 0.0,
                        "Success": 1.0,
                    },
                    dimensions={
                        "Device": result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device)
                    },
                )
            except Exception:
                pass

        except InferenceServerException as e:
            logger.error(f"Triton inference error: {e}")
            result.success = False
            result.error = f"Triton inference error: {e}"
            try:
                emit_emf_metric(
                    namespace="Amira/Inference",
                    metrics={
                        "Success": 0.0,
                    },
                    dimensions={
                        "Device": result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device)
                    },
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error during Triton inference: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            result.success = False
            result.error = str(e)
            try:
                emit_emf_metric(
                    namespace="Amira/Inference",
                    metrics={
                        "Success": 0.0,
                    },
                    dimensions={
                        "Device": result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device)
                    },
                )
            except Exception:
                pass

        return result


def create_inference_engine(*, w2v_config: W2VConfig) -> TritonInferenceEngine:
    """Create a Triton inference engine.

    Args:
        w2v_config: W2V configuration with Triton settings.

    Returns:
        TritonInferenceEngine: The inference engine.
    """
    return TritonInferenceEngine(w2v_config=w2v_config)
