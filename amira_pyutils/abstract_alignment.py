from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from amira_pyutils.language import LanguageHandling

UNK_TOKEN: Final[str] = "xxxx"


class WordSelectionStrategy(StrEnum):
    """Strategy for selecting from multiple aligned words when transcription segments match story words."""

    FIRST = "first"
    LEVENSHTEIN_DISTANCE = "lev"
    GROUPWISE = "group"
    ENSEMBLE = "ensemble"


@dataclass(frozen=True)
class AlignmentConfig:
    """Configuration for text alignment operations."""

    use_production_algorithm: bool
    preprocess_next_expected: bool
    postprocess_inference: bool
    simple_initial_diff: bool = True
    generate_quality_features: bool = False
    word_selection_strategy: WordSelectionStrategy = WordSelectionStrategy.FIRST
    sequence_match_maximal: bool = False

    def is_compatible_with(
        self, *, other: "AlignmentConfig", require_exact_match: bool = True
    ) -> bool:
        """Check compatibility between alignment configurations.

        Args:
            other: Configuration to compare against
            require_exact_match: Whether to require exact alignment strategy match

        Returns:
            True if configurations are compatible
        """
        if other.use_production_algorithm != self.use_production_algorithm:
            return False

        if require_exact_match and (other.word_selection_strategy != self.word_selection_strategy):
            return False

        if not self.use_production_algorithm and (
            other.preprocess_next_expected != self.preprocess_next_expected
            or other.postprocess_inference != self.postprocess_inference
            or other.simple_initial_diff != self.simple_initial_diff
        ):
            return False

        return True

    @classmethod
    def create_default(cls) -> "AlignmentConfig":
        """Create default alignment configuration.

        Returns:
            Default AlignmentConfig instance
        """
        return cls(
            use_production_algorithm=False,
            preprocess_next_expected=False,
            postprocess_inference=False,
        )


@dataclass(frozen=True)
class AlignmentResult:
    """Result of text alignment operation.

    Attributes:
        aligned_reference: The aligned reference text
        aligned_transcription_features: The aligned transcription features
        reference_word_indices: The word indices of the reference text
        transcription_word_indices: The word indices of the transcription text
        raw_aligned_reference: The raw aligned reference text
        raw_aligned_transcription_features: The raw aligned transcription features

    """

    aligned_reference: list[str]
    aligned_transcription_features: list[Any]
    reference_word_indices: list[int]
    transcription_word_indices: list[int]
    raw_aligned_reference: list[str]
    raw_aligned_transcription_features: list[Any]


class AsyncTranscriptionAligner(Protocol):
    """Protocol for asynchronous transcription alignment."""

    async def align(
        self,
        *,
        config: AlignmentConfig,
        language: LanguageHandling,
        reference_text: str,
        transcription_text: str,
        next_expected_word: str | None = None,
    ) -> AlignmentResult:
        """Align reference text with transcription.

        Args:
            config: Alignment configuration
            language: Language handling instance
            reference_text: Reference phrase text
            transcription_text: Transcription phrase text
            next_expected_word: Optional first expected word of following phrase

        Returns:
            Alignment result containing aligned texts and indices
        """
        ...


class BaseAligner(ABC):
    """Abstract base class for text aligners."""

    @abstractmethod
    async def align(
        self,
        *,
        config: AlignmentConfig,
        language: LanguageHandling,
        reference_text: str,
        transcription_text: str,
        next_expected_word: str | None = None,
    ) -> AlignmentResult:
        """Align reference text with transcription.

        Args:
            config: Alignment configuration
            language: Language handling instance
            reference_text: Reference phrase text
            transcription_text: Transcription phrase text
            next_expected_word: Optional first expected word of following phrase

        Returns:
            Alignment result containing aligned texts and indices
        """
        ...
