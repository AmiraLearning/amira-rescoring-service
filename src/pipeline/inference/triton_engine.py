import time
import traceback
from typing import Optional

import numpy as np
from loguru import logger

try:
    import tritonclient.http as httpclient  # type: ignore
    from tritonclient.utils import InferenceServerException  # type: ignore

    TRITON_AVAILABLE = True
except ImportError:
    TRITON_AVAILABLE = False
    logger.warning(
        "Triton client not available. Install with: pip install tritonclient[http]"
    )

from .constants import MS_PER_SECOND, DeviceType
from .models import (
    W2VConfig,
    InferenceInput,
    GPUInferenceResult,
    PhoneticTranscript,
)
from .decoder import PhonemeDecoder


class TritonInferenceEngine:
    """Triton inference engine for remote w2v inference."""

    def __init__(self, *, w2v_config: W2VConfig) -> None:
        if not TRITON_AVAILABLE:
            raise ImportError(
                "Triton client not available. Install with: pip install tritonclient[http]"
            )

        self._w2v_config = w2v_config
        self._decoder = PhonemeDecoder()

        # Initialize Triton client
        try:
            self._client = httpclient.InferenceServerClient(
                url=w2v_config.triton_url.replace("http://", "").replace(
                    "https://", ""
                ),
                verbose=False,
            )

            # Verify server is ready
            if not self._client.is_server_ready():
                raise ConnectionError(
                    f"Triton server not ready at {w2v_config.triton_url}"
                )

            # Verify model is ready
            if not self._client.is_model_ready(w2v_config.triton_model):
                raise ConnectionError(
                    f"Triton model '{w2v_config.triton_model}' not ready"
                )

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
            device=DeviceType.GPU,  # Triton runs on GPU
        )

        try:
            inference_start = time.time()

            # Prepare input for Triton
            preprocess_start = time.time()
            audio_input = input_data.audio_array.astype(np.float32)
            if audio_input.ndim == 1:
                audio_input = audio_input[np.newaxis, :]  # Add batch dimension

            result.preprocess_time_ms = (time.time() - preprocess_start) * MS_PER_SECOND

            # Create Triton input
            triton_input = httpclient.InferInput("INPUT__0", audio_input.shape, "FP32")
            triton_input.set_data_from_numpy(audio_input)

            # Create Triton output request
            triton_output = httpclient.InferRequestedOutput("OUTPUT__0")

            # Run inference
            model_start = time.time()
            response = self._client.infer(
                model_name=self._w2v_config.triton_model,
                inputs=[triton_input],
                outputs=[triton_output],
            )
            result.model_inference_time_ms = (time.time() - model_start) * MS_PER_SECOND

            # Get logits from response
            logits = response.as_numpy("OUTPUT__0")

            # Decode predictions
            decode_start = time.time()
            predicted_ids = np.argmax(logits, axis=-1)

            # For now, we'll do basic decoding without the full processor
            # In a real implementation, you'd want to have the vocabulary/tokenizer available
            result.transcription = ""  # Would need tokenizer to decode properly
            result.pred_tokens = []  # Would need tokenizer to get tokens

            result.decode_time_ms = (time.time() - decode_start) * MS_PER_SECOND

            # Calculate confidence if requested
            if self._w2v_config.include_confidence:
                conf_start = time.time()
                # Apply softmax to get probabilities
                exp_logits = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
                probs = exp_logits / np.sum(exp_logits, axis=-1, keepdims=True)
                max_probs = np.max(probs, axis=-1)[0]  # Remove batch dimension
                result.max_probs = max_probs
                result.confidence_calculation_time_ms = (
                    time.time() - conf_start
                ) * MS_PER_SECOND

            # Create phonetic transcript using decoder
            result.phonetic_transcript = self._decoder.decode(
                pred_tokens=result.pred_tokens,
                max_probs=result.max_probs
                if self._w2v_config.include_confidence
                else None,
            )

            result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
            result.success = True

        except InferenceServerException as e:
            logger.error(f"Triton inference error: {e}")
            result.success = False
            result.error = f"Triton inference error: {e}"
        except Exception as e:
            logger.error(f"Error during Triton inference: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            result.success = False
            result.error = str(e)

        return result


def create_inference_engine(*, w2v_config: W2VConfig) -> TritonInferenceEngine:
    """Create a Triton inference engine.

    Args:
        w2v_config: W2V configuration with Triton settings.

    Returns:
        TritonInferenceEngine: The inference engine.
    """
    return TritonInferenceEngine(w2v_config=w2v_config)
