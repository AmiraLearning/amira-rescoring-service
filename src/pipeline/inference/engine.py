from __future__ import annotations

import os
import time
import traceback
from typing import TYPE_CHECKING

import numpy as np
import asyncio
import torch
from loguru import logger
from transformers import BatchFeature, Wav2Vec2ForCTC, Wav2Vec2Processor

if TYPE_CHECKING:
    from .triton_engine import TritonInferenceEngine

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


async def preload_inference_engine_async(
    *, w2v_config: W2VConfig, warmup: bool = False
):
    """Asynchronously preload the inference engine.

    Args:
        w2v_config: W2V2 configuration to construct the engine with.
        warmup: If True, run a tiny dummy inference to initialize kernels.

    Returns:
        Inference engine instance (either Wav2Vec2InferenceEngine or TritonInferenceEngine).
    """

    def _build() -> "Wav2Vec2InferenceEngine" | "TritonInferenceEngine":
        engine: "Wav2Vec2InferenceEngine" | "TritonInferenceEngine"
        if w2v_config.use_triton:
            from .triton_engine import TritonInferenceEngine

            engine = TritonInferenceEngine(w2v_config=w2v_config)
        else:
            engine = Wav2Vec2InferenceEngine(w2v_config=w2v_config)

        if warmup:
            dummy: np.ndarray = np.zeros(1600, dtype=np.float32)
            engine.infer(
                input_data=InferenceInput(audio_array=dummy, inference_id="warmup")
            )
        return engine

    return await asyncio.to_thread(_build)


class Wav2Vec2InferenceEngine:
    _device: torch.device
    _model: Wav2Vec2ForCTC
    _processor: Wav2Vec2Processor

    def __init__(
        self,
        *,
        w2v_config: W2VConfig,
        model_instance: Wav2Vec2ForCTC | None = None,
        processor_instance: Wav2Vec2Processor | None = None,
    ) -> None:
        """Initialize Wav2Vec2 inference engine.

        Args:
            w2v_config: Configuration for the Wav2Vec2 model
            model_instance: Pre-loaded model instance (optional)
            processor_instance: Pre-loaded processor instance (optional)

        Raises:
            Exception: If model loading or device initialization fails
        """
        self._w2v_config = w2v_config
        logger.info(f"Initializing Wav2Vec2 model from {w2v_config.model_path}...")

        self._processor = (
            processor_instance
            if processor_instance
            else Wav2Vec2Processor.from_pretrained(w2v_config.model_path)
        )

        self._model = (
            model_instance
            if model_instance
            else Wav2Vec2ForCTC.from_pretrained(w2v_config.model_path)
        )

        self._init_device()
        self._decoder = PhonemeDecoder()

    def _init_device(self) -> None:
        """Initialize the device for the model.

        Automatically selects the best available device (CUDA, MPS, or CPU)
        and configures torch threading settings. Applies dynamic quantization
        for CPU deployments when W2V2_QUANTIZE=true.

        Raises:
            Exception: If device initialization or model loading fails
        """
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
                default_threads = max(1, (os.cpu_count() or 8) // 2)
                torch.set_num_threads(default_threads)
            else:
                torch.set_num_threads(int(num_threads_env))
            if not interop_threads_env:
                torch.set_num_interop_threads(2)
            else:
                torch.set_num_interop_threads(int(interop_threads_env))
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to configure torch threading: {e}")

        self._model = self._model.to(self._device)  # type: ignore[arg-type]

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
                logger.warning(
                    f"Dynamic quantization failed, continuing without it: {e}"
                )

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
        if self._device.type == "cuda":
            with torch.autocast(device_type="cuda"):
                logits = self._model(input_values).logits  # type: ignore[operator]
        else:
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
        pred_tokens: list[str] = []
        if hasattr(self._processor, "tokenizer") and self._processor.tokenizer:
            pred_tokens = self._processor.tokenizer.convert_ids_to_tokens(
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
        probs: torch.Tensor = torch.nn.functional.softmax(input=logits, dim=-1)
        max_probs: np.ndarray = torch.max(probs, dim=-1)[0][0].cpu().numpy()
        return ConfidenceResult(
            max_probs=max_probs,
            confidence_time_ms=(time.time() - conf_start) * MS_PER_SECOND,
        )

    def _log_gpu_memory_usage(self) -> None:
        """Log current GPU memory usage statistics.

        Only logs when CUDA is available. Reports both allocated
        and reserved memory in GB format.
        """
        if not torch.cuda.is_available():
            return
        allocated: float = torch.cuda.memory_allocated(device=0) / BYTES_PER_GB
        cached: float = torch.cuda.memory_reserved(device=0) / BYTES_PER_GB
        logger.info(f"GPU: {allocated:.1f}GB allocated, {cached:.1f}GB reserved")

    def infer(self, *, input_data: InferenceInput) -> GPUInferenceResult:
        """Perform the inference.

        Args:
            input_data: The input data.

        Returns:
            GPUInferenceResult: The inference result.
        """
        result: GPUInferenceResult = GPUInferenceResult(
            inference_id=input_data.inference_id
        )
        try:
            if self._device.type == "cuda":
                result.device = DeviceType.GPU
            elif self._device.type == "mps":
                result.device = DeviceType.GPU
            else:
                result.device = DeviceType.CPU
            inference_start: float = time.time()
            preprocess_result: PreprocessResult = self._preprocess_audio(
                audio_array=input_data.audio_array
            )
            result.preprocess_time_ms = preprocess_result.preprocess_time_ms
            with torch.no_grad():
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
    engine: Wav2Vec2InferenceEngine | TritonInferenceEngine | None = None,
) -> GPUInferenceResult:
    """Perform a single audio inference.

    Args:
        audio_array: The audio array.
        w2v_config: The Wav2Vec2 configuration.
        model_instance: The model instance.
        processor_instance: The processor instance.
        inference_id: The inference ID.
        engine: Pre-initialized engine instance (optional).

    Returns:
        GPUInferenceResult: The inference result.
    """
    if engine is None:
        if w2v_config.use_triton:
            from .triton_engine import TritonInferenceEngine

            engine = TritonInferenceEngine(w2v_config=w2v_config)
        else:
            engine = Wav2Vec2InferenceEngine(
                w2v_config=w2v_config,
                model_instance=model_instance,
                processor_instance=processor_instance,
            )
    return engine.infer(
        input_data=InferenceInput(audio_array=audio_array, inference_id=inference_id)
    )
