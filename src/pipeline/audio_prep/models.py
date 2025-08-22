from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field
from utils.audio import DEFAULT_SAMPLING_RATE


class PhraseInput(BaseModel):
    phraseIndex: int
    reference_phonemes: list[str] = Field(default_factory=list)
    expected_text: list[str] = Field(default_factory=list)
    storyId: str | None = None
    studentId: str | None = None


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


class ActivityOutput(BaseModel):
    activityId: str
    phrases: list[ProcessedPhraseOutput] = Field(default_factory=list)
    success: bool = False
    error_message: str | None = None
    phrases_processed: int = 0
    phrases_failed: int = 0
