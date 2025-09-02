from __future__ import annotations

import numpy as np
import torch
from dataclasses import dataclass
from pydantic import BaseModel, Field, field_serializer
from .constants import DeviceType


class W2VConfig(BaseModel):
    """Wav2Vec2 configuration."""

    model_path: str = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"
    device: DeviceType = DeviceType.CPU
    include_confidence: bool = False
    
    # Compilation optimizations
    use_torch_compile: bool = True
    compile_mode: str = "default"  # "default", "reduce-overhead", "max-autotune"
    use_torch_jit: bool = False  # Fallback for torch.compile
    use_mixed_precision: bool = False
    use_flash_attention: bool = False
    
    # Performance optimizations
    use_float16: bool = True  # Lambda-style Float16 precision for 2x speed
    batch_all_phrases: bool = True  # Lambda-style single inference call


class PreprocessResult(BaseModel):
    input_values: torch.Tensor
    preprocess_time_ms: float

    class Config:
        arbitrary_types_allowed = True


class InferenceResult(BaseModel):
    logits: torch.Tensor
    model_inference_time_ms: float

    class Config:
        arbitrary_types_allowed = True


class DecodePredictionResult(BaseModel):
    transcription: str
    pred_tokens: list[str]
    predicted_ids: torch.Tensor
    decode_time_ms: float

    class Config:
        arbitrary_types_allowed = True


class ConfidenceResult(BaseModel):
    max_probs: np.ndarray
    confidence_time_ms: float

    class Config:
        arbitrary_types_allowed = True


class InferenceInput(BaseModel):
    audio_array: np.ndarray
    inference_id: str | None = None

    class Config:
        arbitrary_types_allowed = True


class PhoneticTranscript(BaseModel):
    elements: list[str] = Field(default_factory=list)
    confidences: list[float] = Field(default_factory=list)

    @field_serializer("confidences")
    def _serialize_confidences(self, value: list[float]) -> list[float]:
        return list(value)


class GPUInferenceResult(BaseModel):
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
        if value is None:
            return None
        return value.tolist()

    class Config:
        arbitrary_types_allowed = True


@dataclass(frozen=True)
class GroupedPhoneticUnits:
    tokens: list[str]
    confidences: list[float]


@dataclass(frozen=True)
class Segment:
    tokens: list[str]
    confidences: list[np.ndarray]
