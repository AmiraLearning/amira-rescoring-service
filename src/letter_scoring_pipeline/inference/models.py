"""Models for the inference pipeline."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Self, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

if TYPE_CHECKING:  # pragma: no cover
    pass

from .constants import DeviceType


class W2VConfig(BaseModel):
    """Wav2Vec2 configuration

    Validates Triton configuration to ensure HTTPS-only URLs when Triton is enabled.
    """

    model_path: str = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"
    device: DeviceType = DeviceType.CPU
    include_confidence: bool = False

    # Triton inference server configuration
    # TODO update the docs for this
    use_triton: bool = False
    triton_url: str = "https://localhost:8000"
    triton_model: str = "w2v2"

    # Compilation optimizations
    # TODO update the docs for this
    use_torch_compile: bool = False
    compile_mode: str = "default"  # "default", "reduce-overhead", "max-autotune"
    use_torch_jit: bool = False  # Fallback for torch.compile
    use_jit_trace: bool = True  # JIT tracing for optimized inference (2% speedup)
    use_mixed_precision: bool = False
    use_flash_attention: bool = False

    # Performance optimizations
    use_float16: bool = True  # Lambda-style Float16 precision for 2x speed
    batch_all_phrases: bool = False  # Lambda-style single inference call
    fast_init: bool = False  # Skip expensive optimizations during init for cold start

    # CPU quantization threshold tuning
    quantize_min_cpu_cores: Annotated[int, Field(ge=1, le=16)] = 2
    quantize_skip_on_low_memory: bool = True

    @model_validator(mode="after")
    def _validate_triton_https(self) -> Self:
        """Ensure Triton URL is HTTPS-only when Triton is enabled.

        Returns:
            Self: The validated configuration instance.

        Raises:
            ValueError: If `use_triton` is true and `triton_url` is not a non-empty HTTPS URL.
        """
        if self.use_triton:
            raw: str = self.triton_url
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(
                    "triton_url must be a non-empty https URL when use_triton=true (must use HTTPS)"
                )
            url_lower: str = raw.strip().lower()
            if url_lower.startswith("http://"):
                raise ValueError(
                    "triton_url must use https:// (must use HTTPS), http:// is not allowed"
                )
            if not url_lower.startswith("https://"):
                raise ValueError("triton_url must start with https:// (must use HTTPS)")
        return self


class PreprocessResult(BaseModel):
    """The result of preprocessing the audio.

    Args:
        input_values: The input values.
        preprocess_time_ms: The time taken to preprocess the audio.
    """

    input_values: Any  # Will be torch.Tensor at runtime
    preprocess_time_ms: float
    model_config = ConfigDict(arbitrary_types_allowed=True)


class InferenceResult(BaseModel):
    """The result of the model inference.

    Args:
        logits: The logits.
        model_inference_time_ms: The time taken to run the model inference.
    """

    logits: Any  # Will be torch.Tensor at runtime
    model_inference_time_ms: float
    model_config = ConfigDict(arbitrary_types_allowed=True)


class DecodePredictionResult(BaseModel):
    """The result of decoding the predictions.

    Args:
        transcription: The transcription.
        pred_tokens: The predicted tokens.
        predicted_ids: The predicted ids.
        decode_time_ms: The time taken to decode the predictions.
    """

    transcription: str
    pred_tokens: list[str]
    predicted_ids: Any  # Will be torch.Tensor at runtime
    decode_time_ms: float
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ConfidenceResult(BaseModel):
    """The result of calculating the confidence.

    Args:
        max_probs: The max probabilities.
        confidence_time_ms: The time taken to calculate the confidence.
    """

    max_probs: np.ndarray
    confidence_time_ms: float
    model_config = ConfigDict(arbitrary_types_allowed=True)


class InferenceInput(BaseModel):
    """The input for the inference.

    Args:
        audio_array: The audio array.
        inference_id: The inference id.
        correlation_id: The correlation id.
    """

    audio_array: np.ndarray
    inference_id: str | None = None
    correlation_id: str | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class PhoneticTranscript(BaseModel):
    """The phonetic transcript.

    Args:
        elements: The elements.
        confidences: The confidences.
    """

    elements: list[str] = Field(default_factory=list)
    confidences: list[float] = Field(default_factory=list)

    @field_serializer("confidences")
    def _serialize_confidences(self, value: list[float]) -> list[float]:
        """Serialize the confidences.

        Args:
            value: The value to serialize.

        Returns:
            The serialized value.
        """
        return list(value)


class GPUInferenceResult(BaseModel):
    """The result of the GPU inference.

    Args:
        transcription: The transcription.
        pred_tokens: The predicted tokens.
        max_probs: The max probabilities.
        phonetic_transcript: The phonetic transcript.
        total_duration_ms: The total duration of the inference.
        preprocess_time_ms: The time taken to preprocess the audio.
        model_inference_time_ms: The time taken to run the model inference.
        decode_time_ms: The time taken to decode the predictions.
        confidence_calculation_time_ms: The time taken to calculate the confidence.
        device: The device.
        success: The success.
        error: The error.
        inference_id: The inference id.
    """

    transcription: str | None = None
    pred_tokens: list[str] = Field(default_factory=list)
    max_probs: np.ndarray | None = None
    phonetic_transcript: PhoneticTranscript = Field(default_factory=PhoneticTranscript)
    total_duration_ms: float = 0.0
    preprocess_time_ms: float = 0.0
    model_inference_time_ms: float = 0.0
    decode_time_ms: float = 0.0
    confidence_calculation_time_ms: float = 0.0
    device: DeviceType = DeviceType.CPU
    success: bool = False
    error: str | None = None
    inference_id: str | None = None

    @field_serializer("max_probs")
    def _serialize_max_probs(self, value: np.ndarray | None) -> list[float] | None:
        """Serialize the max probabilities.

        Args:
            value: The value to serialize.

        Returns:
            The serialized value.
        """
        if value is None:
            return None
        return cast(list[float], value.tolist())

    model_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(frozen=True)
class GroupedPhoneticUnits:
    """The grouped phonetic units.

    Args:
        tokens: The tokens.
        confidences: The confidences.
    """

    tokens: list[str]
    confidences: list[float]


@dataclass(frozen=True)
class Segment:
    """The segment.

    Args:
        tokens: The tokens.
        confidences: The confidences.
    """

    tokens: list[str]
    confidences: list[np.ndarray]
