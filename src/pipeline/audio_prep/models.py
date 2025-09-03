from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from utils.audio import DEFAULT_SAMPLING_RATE


class PhraseInput(BaseModel):
    phraseIndex: int
    reference_phonemes: list[str] = Field(default_factory=list)
    expected_text: list[str] = Field(default_factory=list)
    storyId: str | None = None
    studentId: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhraseInput:
        # Extract only the fields that PhraseInput expects
        filtered_data = {
            "phraseIndex": data["phraseIndex"],
            "reference_phonemes": data.get("reference_phonemes", []),
            "expected_text": data.get("expected_text", []),
            "storyId": data.get("storyId"),
            "studentId": data.get("studentId"),
        }
        return cls(**filtered_data)


class ActivityInput(BaseModel):
    activityId: str
    phrases: list[PhraseInput]


class ProcessedPhraseOutput(BaseModel):
    activityId: str
    phraseIndex: int
    speech: np.ndarray
    sampling_rate: int = DEFAULT_SAMPLING_RATE
    reference_phonemes: list[str] = Field(default_factory=list)
    expected_text: list[str] = Field(default_factory=list)
    storyId: str | None = None
    studentId: str | None = None

    class Config:
        arbitrary_types_allowed = True


class ActivityOutput(BaseModel):
    activityId: str
    phrases: list[ProcessedPhraseOutput] = Field(default_factory=list)
    success: bool = False
    error_message: str | None = None
    phrases_processed: int = 0
    phrases_failed: int = 0

    class Config:
        arbitrary_types_allowed = True
