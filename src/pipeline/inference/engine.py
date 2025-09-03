import asyncio
import os
import time
import traceback
from typing import TYPE_CHECKING, Any

import numpy as np
import torch
from loguru import logger
from transformers import BatchFeature, Wav2Vec2ForCTC, Wav2Vec2Processor

from utils.logging import emit_emf_metric

if TYPE_CHECKING:
    from .triton_engine import TritonInferenceEngine

from .constants import (
    BYTES_PER_GB,
    MS_PER_SECOND,
    DeviceType,
)
from .decoder import PhonemeDecoder
from .models import (
    ConfidenceResult,
    DecodePredictionResult,
    GPUInferenceResult,
    InferenceInput,
    InferenceResult,
    PreprocessResult,
    W2VConfig,
)

_cached_engines: dict[str, "Wav2Vec2InferenceEngine | TritonInferenceEngine"] = {}


async def preload_inference_engine_async(
    *, w2v_config: W2VConfig, warmup: bool = False
) -> "Wav2Vec2InferenceEngine | TritonInferenceEngine":
    """Asynchronously preload the inference engine.

    Args:
        w2v_config: W2V2 configuration to construct the engine with.
        warmup: If True, run a tiny dummy inference to initialize kernels.

    Returns:
        Inference engine instance (either Wav2Vec2InferenceEngine or TritonInferenceEngine).
    """

    def _build() -> "Wav2Vec2InferenceEngine | TritonInferenceEngine":
        engine: Any
        if w2v_config.use_triton:
            from .triton_engine import TritonInferenceEngine

            engine = TritonInferenceEngine(w2v_config=w2v_config)
        else:
            engine = Wav2Vec2InferenceEngine(w2v_config=w2v_config)

        if warmup:
            dummy: np.ndarray = np.zeros(1600, dtype=np.float32)
            engine.infer(input_data=InferenceInput(audio_array=dummy, inference_id="warmup"))
        return engine

    return await asyncio.to_thread(_build)


class Wav2Vec2InferenceEngine:
    _device: torch.device
    _model: Wav2Vec2ForCTC
    _processor: Wav2Vec2Processor
    _traced_model: torch.jit.ScriptModule | None

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
        self._traced_model = None
        logger.info(f"Initializing Wav2Vec2 model from {w2v_config.model_path}...")

        # For cold start optimization, load model and processor in parallel
        if model_instance is None or processor_instance is None:
            self._model, self._processor = self._load_model_and_processor_parallel()
            # Use provided instances if available
            if model_instance is not None:
                self._model = model_instance
            if processor_instance is not None:
                self._processor = processor_instance
        else:
            self._model = model_instance
            self._processor = processor_instance

        self._init_device()
        self._decoder = PhonemeDecoder()

    def _load_model_and_processor_parallel(self) -> tuple[Wav2Vec2ForCTC, Wav2Vec2Processor]:
        """Load model and processor in parallel for faster cold start.

        Returns:
            Tuple of (model, processor) loaded concurrently
        """
        import concurrent.futures

        logger.info("Loading model and processor in parallel...")
        start_time = time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both loading tasks
            model_future = executor.submit(
                Wav2Vec2ForCTC.from_pretrained,
                self._w2v_config.model_path,
                use_safetensors=True,  # Use safetensors if available
            )
            processor_future = executor.submit(
                Wav2Vec2Processor.from_pretrained, self._w2v_config.model_path
            )

            # Wait for both to complete
            model = model_future.result()
            processor = processor_future.result()

        load_time = time.time() - start_time
        logger.info(f"Parallel loading completed in {load_time:.2f}s")
        return model, processor

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
                try:
                    default_threads = max(1, (os.cpu_count() or 8) // 2)
                    torch.set_num_threads(default_threads)
                except RuntimeError:
                    pass
            else:
                try:
                    torch.set_num_threads(int(num_threads_env))
                except RuntimeError:
                    pass
            if not interop_threads_env:
                try:
                    torch.set_num_interop_threads(2)
                except RuntimeError:
                    pass
            else:
                try:
                    torch.set_num_interop_threads(int(interop_threads_env))
                except RuntimeError:
                    pass
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to configure torch threading: {e}")

        self._model = self._model.to(self._device)

        if self._w2v_config.use_torch_compile:
            try:
                compile_mode = self._w2v_config.compile_mode or "default"
                compiled_model = torch.compile(self._model, mode=compile_mode)
                self._model = compiled_model
                logger.info(f"Enabled torch.compile with mode={compile_mode}")
            except Exception as e:
                logger.warning(f"torch.compile unavailable or failed; continuing without it: {e}")

        if self._device.type == "cpu" and os.getenv("W2V2_QUANTIZE", "false").lower() == "true":
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

        # Skip expensive optimizations if fast_init is enabled (cold start optimization)
        if self._w2v_config.use_jit_trace and not self._w2v_config.fast_init:
            self._init_jit_trace()
        elif self._w2v_config.fast_init:
            logger.info("Skipping JIT trace for fast initialization")

    def _init_jit_trace(self) -> None:
        """Initialize JIT traced model for optimized inference."""
        try:
            dummy_input: torch.Tensor = torch.zeros((1, 16000), device=self._device)
            self._traced_model = torch.jit.trace(self._model, dummy_input)
            logger.info("JIT trace optimization enabled")
        except Exception as e:
            logger.warning(f"JIT trace failed, using regular model: {e}")
            self._traced_model = None

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
        input_values: torch.Tensor = inputs.input_values.to(self._device, non_blocking=True)
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
        logger.debug(f"Starting model inference on device: {self._device.type}")

        model_to_use = self._traced_model if self._traced_model is not None else self._model

        use_amp: bool = bool(getattr(self._w2v_config, "use_mixed_precision", False))
        if self._device.type == "cuda" and use_amp:
            with torch.autocast(device_type="cuda"):
                logger.debug("Running model with CUDA autocast")
                logits = model_to_use(input_values)
                if not isinstance(logits, torch.Tensor):
                    logits = logits.logits
        elif self._device.type == "mps" and use_amp and hasattr(torch, "autocast"):
            try:
                with torch.autocast(device_type="mps"):
                    logger.debug("Running model with MPS autocast")
                    logits = model_to_use(input_values)
                    if not isinstance(logits, torch.Tensor):
                        logits = logits.logits
            except Exception:
                logger.debug("MPS autocast unavailable; running without autocast")
                logits = model_to_use(input_values)
                if not isinstance(logits, torch.Tensor):
                    logits = logits.logits
        else:
            logger.debug("Running model without autocast")
            logits = model_to_use(input_values)
            if not isinstance(logits, torch.Tensor):
                logits = logits.logits
        logger.debug(f"Model inference completed in {(time.time() - model_start) * 1000:.1f}ms")
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

        predicted_ids_cpu = predicted_ids[0].cpu()
        pred_tokens: list[str] = []
        if hasattr(self._processor, "tokenizer") and self._processor.tokenizer:
            pred_tokens = self._processor.tokenizer.convert_ids_to_tokens(predicted_ids_cpu.numpy())
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
        with torch.inference_mode():
            max_probs_tensor = torch.max(torch.nn.functional.softmax(logits, dim=-1), dim=-1)[0][0]
            max_probs: np.ndarray = max_probs_tensor.cpu().numpy()
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
        result: GPUInferenceResult = GPUInferenceResult(inference_id=input_data.inference_id)
        try:
            if self._device.type == "cuda":
                result.device = DeviceType.GPU
            elif self._device.type == "mps":
                result.device = DeviceType.MPS
            else:
                result.device = DeviceType.CPU
            inference_start: float = time.time()
            preprocess_result: PreprocessResult = self._preprocess_audio(
                audio_array=input_data.audio_array
            )
            result.preprocess_time_ms = preprocess_result.preprocess_time_ms
            # inference_mode has lower overhead than no_grad for pure inference
            with torch.inference_mode():
                inference_result: InferenceResult = self._run_model_inference(
                    input_values=preprocess_result.input_values
                )
                result.model_inference_time_ms = inference_result.model_inference_time_ms
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
                    result.confidence_calculation_time_ms = conf_result.confidence_time_ms
                result.phonetic_transcript = self._decoder.decode(
                    pred_tokens=result.pred_tokens,
                    max_probs=(result.max_probs if self._w2v_config.include_confidence else None),
                )
            result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
            result.success = True
            self._log_gpu_memory_usage()
            try:
                emit_emf_metric(
                    namespace="Amira/Inference",
                    metrics={
                        "InferenceTotalMs": result.total_duration_ms,
                        "PreprocessMs": result.preprocess_time_ms,
                        "ModelMs": result.model_inference_time_ms,
                        "DecodeMs": result.decode_time_ms,
                    },
                    dimensions={
                        "Device": result.device.value
                        if hasattr(result.device, "value")
                        else str(result.device),
                        "IncludeConfidence": str(self._w2v_config.include_confidence).lower(),
                        **(
                            {"CorrelationId": str(input_data.inference_id)}
                            if getattr(input_data, "inference_id", None)
                            else {}
                        ),
                    },
                )
            except Exception:
                pass
        except Exception as e:  # pragma: no cover
            logger.error(f"Error during inference: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")

            # TODO ugly code, but it's a workaround for a known issue with MPS
            # If MPS fails with convolution error, try fallback to CPU
            if "convolution_overrideable not implemented" in str(e) and self._device.type == "mps":
                logger.warning("MPS convolution failed, falling back to CPU for this inference")
                try:
                    # Temporarily move model to CPU
                    original_device = self._device
                    self._device = torch.device("cpu")
                    self._model = self._model.to(self._device)

                    # Retry inference on CPU
                    inference_start = time.time()
                    preprocess_result = self._preprocess_audio(audio_array=input_data.audio_array)
                    result.preprocess_time_ms = preprocess_result.preprocess_time_ms

                    with torch.inference_mode():
                        inference_result = self._run_model_inference(
                            input_values=preprocess_result.input_values
                        )
                        result.model_inference_time_ms = inference_result.model_inference_time_ms
                        decode_result = self._decode_predictions(logits=inference_result.logits)
                        result.decode_time_ms = decode_result.decode_time_ms
                        result.transcription = decode_result.transcription
                        result.pred_tokens = decode_result.pred_tokens
                        result.phonetic_transcript = self._decoder.decode(
                            pred_tokens=result.pred_tokens, max_probs=None
                        )

                    result.total_duration_ms = (time.time() - inference_start) * MS_PER_SECOND
                    result.success = True

                    # Move model back to original device
                    self._device = original_device
                    self._model = self._model.to(self._device)
                    logger.info("CPU fallback inference successful")

                except Exception as fallback_e:
                    logger.error(f"CPU fallback also failed: {fallback_e}")
                    result.success = False
                    result.error = str(e)  # Original error
            else:
                result.success = False
                result.error = str(e)
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        return result


def get_cached_engine(
    *, w2v_config: W2VConfig
) -> "Wav2Vec2InferenceEngine | TritonInferenceEngine":
    """Get or create a cached inference engine for Lambda optimization.

    Args:
        w2v_config: The Wav2Vec2 configuration.

    Returns:
        Cached inference engine instance.
    """
    cache_key = f"{w2v_config.model_path}_{w2v_config.use_triton}_{w2v_config.use_torch_compile}_{w2v_config.use_jit_trace}"

    if cache_key not in _cached_engines:
        if w2v_config.use_triton:
            from .triton_engine import TritonInferenceEngine

            _cached_engines[cache_key] = TritonInferenceEngine(w2v_config=w2v_config)
        else:
            _cached_engines[cache_key] = Wav2Vec2InferenceEngine(w2v_config=w2v_config)
        logger.info(f"Cached new inference engine with key: {cache_key}")

    return _cached_engines[cache_key]


def perform_single_audio_inference(
    *,
    audio_array: np.ndarray,
    w2v_config: W2VConfig,
    model_instance: Wav2Vec2ForCTC | None = None,
    processor_instance: Wav2Vec2Processor | None = None,
    inference_id: str | None = None,
    engine: "Wav2Vec2InferenceEngine | TritonInferenceEngine | None" = None,
    use_cache: bool = False,
) -> GPUInferenceResult:
    """Perform a single audio inference.

    Args:
        audio_array: The audio array.
        w2v_config: The Wav2Vec2 configuration.
        model_instance: The model instance.
        processor_instance: The processor instance.
        inference_id: The inference ID.
        engine: Pre-initialized engine instance (optional).
        use_cache: Whether to use global engine caching (useful for Lambda).

    Returns:
        GPUInferenceResult: The inference result.
    """
    if engine is None:
        if use_cache:
            engine = get_cached_engine(w2v_config=w2v_config)
        elif w2v_config.use_triton:
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
