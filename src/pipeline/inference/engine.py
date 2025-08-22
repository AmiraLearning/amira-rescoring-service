from __future__ import annotations

import os
import time
import traceback
from typing import Any

import numpy as np
import asyncio
import torch
from loguru import logger
from transformers import BatchFeature, Wav2Vec2ForCTC, Wav2Vec2Processor
import tritonclient.http as triton_http

from .constants import (
    BYTES_PER_GB,
    MS_PER_SECOND,
    DeviceType,
)
from .models import (
    W2VConfig,
    PreprocessResult,
    InferenceResult,
    DecodePredictionResult,
    ConfidenceResult,
    InferenceInput,
    GPUInferenceResult,
)
from .decoder import PhonemeDecoder


_ENGINE_SINGLETON: Wav2Vec2InferenceEngine | None = None


async def preload_inference_engine_async(
    *, w2v_config: W2VConfig, warmup: bool = False
) -> None:
    """Asynchronously preload the inference engine (and optionally warm it up).

    Args:
        w2v_config: W2V2 configuration to construct the engine with.
        warmup: If True, run a tiny dummy inference to initialize kernels.
    """
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is not None:
        return

    def _build() -> None:
        global _ENGINE_SINGLETON
        if _ENGINE_SINGLETON is None:
            _ENGINE_SINGLETON = Wav2Vec2InferenceEngine(w2v_config=w2v_config)
            if warmup:
                dummy: np.ndarray = np.zeros(1600, dtype=np.float32)
                _ENGINE_SINGLETON.infer(
                    input_data=InferenceInput(audio_array=dummy, inference_id="warmup")
                )

    await asyncio.to_thread(_build)


class Wav2Vec2InferenceEngine:
    def __init__(
        self,
        *,
        w2v_config: W2VConfig,
        model_instance: Wav2Vec2ForCTC | None = None,
        processor_instance: Wav2Vec2Processor | None = None,
    ) -> None:
        self._w2v_config = w2v_config
        logger.info(f"Initializing Wav2Vec2 model from {w2v_config.model_path}...")

        self._use_triton: bool = (
            os.getenv("USE_TRITON", "false").lower() == "true"
            and triton_http is not None
        )
        self._triton_client = None
        self._triton_model_name = None

        self._processor = (
            processor_instance
            if processor_instance
            else Wav2Vec2Processor.from_pretrained(w2v_config.model_path)
        )

        if self._use_triton:
            self._triton_client = triton_http.InferenceServerClient(
                url=os.getenv("TRITON_URL", "http://localhost:8000"), verbose=False
            )
            self._triton_model_name = os.getenv("TRITON_MODEL", "w2v2")
            self._model = None
            logger.info(
                f"Using Triton inference at {os.getenv('TRITON_URL', 'http://localhost:8000')} model {self._triton_model_name}"
            )
        else:
            self._model = (
                model_instance
                if model_instance
                else Wav2Vec2ForCTC.from_pretrained(w2v_config.model_path)
            )

        self._init_device()
        self._decoder = PhonemeDecoder()

    def _init_device(self) -> None:
        """Initialize the device for the model."""
        if torch.cuda.is_available():
            self._device = torch.device(DeviceType.GPU.value)
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self._device = torch.device("mps")
        else:
            self._device = torch.device(DeviceType.CPU.value)

        try:
            num_threads_env = os.getenv("TORCH_NUM_THREADS")
            interop_threads_env = os.getenv("TORCH_NUM_INTEROP_THREADS")
            if not num_threads_env:
                import os as _os
                default_threads = max(1, (_os.cpu_count() or 8) // 2)
                torch.set_num_threads(default_threads)
            else:
                torch.set_num_threads(int(num_threads_env))
            if not interop_threads_env:
                torch.set_num_interop_threads(2)
            else:
                torch.set_num_interop_threads(int(interop_threads_env))
        except Exception:
            pass

        self._model = self._model.to(self._device)

        if (
            self._device.type == "cpu"
            and os.getenv("W2V2_QUANTIZE", "false").lower() == "true"
        ):
            try:
                self._model = torch.quantization.quantize_dynamic(
                    self._model, {torch.nn.Linear}, dtype=torch.qint8
                )
                logger.info("Applied dynamic quantization to W2V2 model (CPU)")
            except Exception as e:
                logger.warning(f"Dynamic quantization failed, continuing without it: {e}")

        self._model.eval()
        logger.info(f"Model loaded on {self._device}")
        self._log_gpu_memory_usage()

    def _preprocess_audio(self, *, audio_array: np.ndarray) -> PreprocessResult:
        """Preprocess the audio array.

        Args:
            audio_array: The audio array to preprocess.

        Returns:
            PreprocessResult: The preprocessed audio array.
        """
        preprocess_start: float = time.time()
        inputs: BatchFeature = self._processor(
            [audio_array], sampling_rate=16_000, return_tensors="pt", padding=False
        )
        input_values: torch.Tensor = inputs.input_values.to(self._device)
        return PreprocessResult(
            input_values=input_values,
            preprocess_time_ms=(time.time() - preprocess_start) * MS_PER_SECOND,
        )

    def _run_model_inference(self, *, input_values: torch.Tensor) -> InferenceResult:
        """Run the model inference.

        Args:
            input_values: The input values to run the model inference on.

        Returns:
            InferenceResult: The inference result.
        """
        model_start: float = time.time()
        if (
            self._use_triton
            and self._triton_client is not None
            and self._triton_model_name is not None
        ):
            inp = triton_http.InferInput("INPUT__0", list(input_values.shape), "FP32")
            inp.set_data_from_numpy(
                input_values.detach().cpu().numpy(), binary_data=True
            )
            out = triton_http.InferRequestedOutput("OUTPUT__0", binary_data=True)
            resp = self._triton_client.infer(
                model_name=self._triton_model_name, inputs=[inp], outputs=[out]
            )
            logits_np = resp.as_numpy("OUTPUT__0")
            logits: torch.Tensor = torch.from_numpy(logits_np)
        else:
            logger.info(
                f"Running w2v2 model inference on {input_values.shape} input values"
            )
            with torch.autocast(device_type=self._device.type if self._device.type != "mps" else "cpu", enabled=(self._device.type != "cpu")):
                logits = self._model(input_values).logits  # type: ignore[operator]
        return InferenceResult(
            logits=logits,
            model_inference_time_ms=(time.time() - model_start) * MS_PER_SECOND,
        )

    def _decode_predictions(self, *, logits: torch.Tensor) -> DecodePredictionResult:
        """Decode the predictions.

        Args:
            logits: The logits to decode.

        Returns:
            DecodePredictionResult: The decoded predictions.
        """
        decode_start: float = time.time()
        predicted_ids: torch.Tensor = torch.argmax(logits, dim=-1)
        pred_tokens: list[str] = self._processor.tokenizer.convert_ids_to_tokens(
            predicted_ids[0].cpu().numpy()
        )
        transcription: str = self._processor.batch_decode(predicted_ids)[0]
        return DecodePredictionResult(
            transcription=transcription,
            pred_tokens=pred_tokens,
            predicted_ids=predicted_ids,
            decode_time_ms=(time.time() - decode_start) * MS_PER_SECOND,
        )

    def _calculate_confidence(self, *, logits: torch.Tensor) -> ConfidenceResult:
        """Calculate the confidence.

        Args:
            logits: The logits to calculate the confidence on.

        Returns:
            ConfidenceResult: The confidence result.
        """
        conf_start: float = time.time()
        probs: torch.Tensor = torch.nn.functional.softmax(logits=logits, dim=-1)
        max_probs: np.ndarray = torch.max(probs, dim=-1)[0][0].cpu().numpy()
        return ConfidenceResult(
            max_probs=max_probs,
            confidence_time_ms=(time.time() - conf_start) * MS_PER_SECOND,
        )

    def _log_gpu_memory_usage(self) -> None:
        """Log the GPU memory usage."""
        if not torch.cuda.is_available():
            logger.info("No GPU available")
            return
        allocated = torch.cuda.memory_allocated(device=0) / BYTES_PER_GB
        cached = torch.cuda.memory_reserved(device=0) / BYTES_PER_GB
        logger.info(f"GPU: {allocated:.1f}GB allocated, {cached:.1f}GB reserved")

    def infer(self, *, input_data: InferenceInput) -> GPUInferenceResult:
        """Perform the inference.

        Args:
            input_data: The input data.

        Returns:
            GPUInferenceResult: The inference result.
        """
        logger.info(
            f"Performing inference on {input_data.audio_array.shape} audio array"
        )
        result: GPUInferenceResult = GPUInferenceResult(
            inference_id=input_data.inference_id
        )
        try:
            result.device = (
                DeviceType.GPU if self._device.type == "cuda" else DeviceType.CPU
            )
            inference_start: float = time.time()
            preprocess_result: PreprocessResult = self._preprocess_audio(
                audio_array=input_data.audio_array
            )
            result.preprocess_time_ms = preprocess_result.preprocess_time_ms
            with torch.no_grad():
                logger.info(
                    f"Running model inference on {preprocess_result.input_values.shape} input values"
                )
                inference_result: InferenceResult = self._run_model_inference(
                    input_values=preprocess_result.input_values
                )
                result.model_inference_time_ms = (
                    inference_result.model_inference_time_ms
                )
                decode_result: DecodePredictionResult = self._decode_predictions(
                    logits=inference_result.logits
                )
                result.decode_time_ms = decode_result.decode_time_ms
                result.transcription = decode_result.transcription
                result.pred_tokens = decode_result.pred_tokens
                if self._w2v_config.include_confidence:
                    conf_result: ConfidenceResult = self._calculate_confidence(
                        logits=inference_result.logits
                    )
                    result.max_probs = conf_result.max_probs
                    result.confidence_calculation_time_ms = (
                        conf_result.confidence_time_ms
                    )
                result.phonetic_transcript = self._decoder.decode(
                    pred_tokens=result.pred_tokens,
                    max_probs=(
                        result.max_probs
                        if self._w2v_config.include_confidence
                        else None
                    ),
                )
            result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
            result.success = True
            self._log_gpu_memory_usage()
        except Exception as e:  # pragma: no cover
            logger.error(f"Error during inference: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            result.success = False
            result.error = str(e)
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return result


def perform_single_audio_inference(
    *,
    audio_array: np.ndarray,
    w2v_config: W2VConfig,
    model_instance: Wav2Vec2ForCTC | None = None,
    processor_instance: Wav2Vec2Processor | None = None,
    inference_id: str | None = None,
) -> GPUInferenceResult:
    """Perform a single audio inference.

    Args:
        audio_array: The audio array.
        w2v_config: The Wav2Vec2 configuration.
        model_instance: The model instance.
        processor_instance: The processor instance.
        inference_id: The inference ID.

    Returns:
        GPUInferenceResult: The inference result.
    """
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is None:
        _ENGINE_SINGLETON = Wav2Vec2InferenceEngine(
            w2v_config=w2v_config,
            model_instance=model_instance,
            processor_instance=processor_instance,
        )
    engine: Wav2Vec2InferenceEngine = _ENGINE_SINGLETON
    return engine.infer(
        input_data=InferenceInput(audio_array=audio_array, inference_id=inference_id)
    )
