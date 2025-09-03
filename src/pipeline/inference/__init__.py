from typing import Any

from .constants import (
    BYTES_PER_GB,
    MS_PER_SECOND,
    VALID_PHONETIC_ELEMENTS,
    DeviceType,
    TokenType,
)
from .models import (
    ConfidenceResult,
    DecodePredictionResult,
    GPUInferenceResult,
    InferenceInput,
    InferenceResult,
    PhoneticTranscript,
    PreprocessResult,
    W2VConfig,
)

__all__ = [
    "BYTES_PER_GB",
    "MS_PER_SECOND",
    "VALID_PHONETIC_ELEMENTS",
    "ConfidenceResult",
    "DecodePredictionResult",
    "DeviceType",
    "GPUInferenceResult",
    "InferenceInput",
    "InferenceResult",
    "PhoneticTranscript",
    "PreprocessResult",
    "TokenType",
    "W2VConfig",
    "Wav2Vec2InferenceEngine",
    "perform_single_audio_inference",
]


def __getattr__(name: str) -> Any:
    if name in {"Wav2Vec2InferenceEngine", "perform_single_audio_inference"}:
        from .engine import Wav2Vec2InferenceEngine, perform_single_audio_inference

        return {
            "Wav2Vec2InferenceEngine": Wav2Vec2InferenceEngine,
            "perform_single_audio_inference": perform_single_audio_inference,
        }[name]
    raise AttributeError(name)
