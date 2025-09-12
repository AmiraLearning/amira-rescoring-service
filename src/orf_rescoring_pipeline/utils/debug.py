from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from loguru import logger


class FlaggingStrategy(StrEnum):
    """Strategies for how to handle phrase flagging."""

    KEEP_ORIGINAL = "keep_original"
    MARK_ALL_ERRORS = "mark_all_errors"
    RESCORE_PHRASE = "rescore_phrase"


@dataclass(frozen=True)
class FlaggingDecision:
    """Decision about how to flag a phrase, with reasoning."""

    strategy: FlaggingStrategy
    reason: str


@dataclass(frozen=True)
class AccuracyStats:
    """Accuracy statistics for an activity."""

    original_accuracy: float
    retouched_accuracy: float
    total_words: int
    improvement: float

    @classmethod
    def from_counts(
        cls,
        *,
        correct_original: int,
        correct_retouched: int,
        total_words: int,
    ) -> "AccuracyStats":
        """Create accuracy stats from raw counts."""
        original_accuracy = correct_original / total_words if total_words > 0 else 0.0
        retouched_accuracy = correct_retouched / total_words if total_words > 0 else 0.0
        improvement = retouched_accuracy - original_accuracy

        return cls(
            original_accuracy=original_accuracy,
            retouched_accuracy=retouched_accuracy,
            total_words=total_words,
            improvement=improvement,
        )


@dataclass(frozen=True)
class PhraseDebugInfo:
    """Debug information for a single phrase."""

    phrase_index: int
    story_phrase: str
    story_words: list[str]
    decision: FlaggingDecision
    deepgram_matches: list[int]
    original_kaldi_matches: list[int]
    original_w2v_matches: list[int]
    rescored_kaldi_matches: list[int] | None
    rescored_w2v_matches: list[int] | None
    kaldi_transcript: str | None
    w2v_transcript: str | None
    model_predictions: list[bool]
    original_errors: list[bool]
    retouched_errors: list[bool]
    phrase_words: int
    phrase_correct_model: int
    phrase_correct_retouched: int


class ActivityDebugger:
    """Collects and formats debug information for detailed word-level analysis."""

    def __init__(self, *, activity: Any) -> None:
        self.activity = activity
        self.phrase_debug_data: list[PhraseDebugInfo] = []
        self.total_words = 0
        self.total_correct_original = 0
        self.total_correct_retouched = 0

    def add_phrase_debug_info(
        self,
        *,
        feature: Any,
        decision: FlaggingDecision,
        original_errors: list[bool],
        retouched_errors: list[bool],
        story_phrase: str,
        kaldi_transcript: str | None = None,
        w2v_transcript: str | None = None,
    ) -> None:
        """Add debug information for a phrase."""
        story_words = story_phrase.split()

        deepgram_matches = self._extract_matches(
            matches=feature.deepgram_match, target_length=len(story_words)
        )

        original_kaldi_matches = self._extract_and_convert_matches(
            matches=getattr(feature, "kaldi_match", []), target_length=len(story_words)
        )

        original_w2v_matches = self._extract_and_convert_matches(
            matches=getattr(feature, "w2v_match", []), target_length=len(story_words)
        )

        rescored_kaldi_matches = self._extract_rescored_matches(
            matches=getattr(feature, "resliced_kaldi_match", None), target_length=len(story_words)
        )

        rescored_w2v_matches = self._extract_rescored_matches(
            matches=getattr(feature, "resliced_w2v_match", None), target_length=len(story_words)
        )

        model_predictions = self.activity.model_predictions[feature.phrase_index]

        phrase_words = min(
            len(story_words),
            len(original_errors),
            len(retouched_errors),
            len(model_predictions),
        )

        phrase_correct_original, phrase_correct_retouched = self._calculate_phrase_accuracy(
            model_predictions=model_predictions,
            original_errors=original_errors,
            retouched_errors=retouched_errors,
            phrase_words=phrase_words,
        )

        debug_info = PhraseDebugInfo(
            phrase_index=feature.phrase_index,
            story_phrase=story_phrase,
            story_words=story_words,
            decision=decision,
            deepgram_matches=deepgram_matches,
            original_kaldi_matches=original_kaldi_matches,
            original_w2v_matches=original_w2v_matches,
            rescored_kaldi_matches=rescored_kaldi_matches,
            rescored_w2v_matches=rescored_w2v_matches,
            kaldi_transcript=kaldi_transcript,
            w2v_transcript=w2v_transcript,
            model_predictions=model_predictions[:phrase_words],
            original_errors=original_errors[:phrase_words],
            retouched_errors=retouched_errors[:phrase_words],
            phrase_words=phrase_words,
            phrase_correct_model=phrase_correct_original,
            phrase_correct_retouched=phrase_correct_retouched,
        )

        self.phrase_debug_data.append(debug_info)
        self._update_totals(
            phrase_words=phrase_words,
            phrase_correct_original=phrase_correct_original,
            phrase_correct_retouched=phrase_correct_retouched,
        )

    def save_debug_file(self, *, output_dir: str = "debug_output") -> str:
        """Save detailed debug information to a text file."""
        Path(output_dir).mkdir(exist_ok=True)
        output_file = Path(output_dir) / f"{self.activity.activity_id}_debug.txt"

        with open(output_file, "w") as f:
            self._write_debug_header(f=f)
            self._write_debug_summary(f=f)
            self._write_phrase_analysis(f=f)

        logger.info(f"Debug file saved: {output_file}")
        return str(output_file)

    def get_activity_accuracy_stats(self) -> AccuracyStats:
        """Get activity-level accuracy statistics."""
        return AccuracyStats.from_counts(
            correct_original=self.total_correct_original,
            correct_retouched=self.total_correct_retouched,
            total_words=self.total_words,
        )

    def _extract_matches(self, *, matches: list[Any], target_length: int) -> list[int]:
        """Extract matches ensuring proper length."""
        return matches[:target_length]

    def _extract_and_convert_matches(self, *, matches: list[Any], target_length: int) -> list[int]:
        """Extract and convert matches to integers, handling various data types."""
        raw_matches = matches[:target_length] if matches else [0] * target_length
        return [self._convert_to_int(value=x) for x in raw_matches]

    def _extract_rescored_matches(
        self, *, matches: list[Any] | None, target_length: int
    ) -> list[int] | None:
        """Extract rescored matches if available."""
        if matches is None:
            return None
        return [int(x) for x in matches[:target_length]]

    def _convert_to_int(self, *, value: Any) -> int:
        """Convert various data types to integer."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float):
            return int(float(value))
        return 0

    def _calculate_phrase_accuracy(
        self,
        *,
        model_predictions: list[bool],
        original_errors: list[bool],
        retouched_errors: list[bool],
        phrase_words: int,
    ) -> tuple[int, int]:
        """Calculate phrase-level accuracy counts."""
        phrase_correct_original = 0
        phrase_correct_retouched = 0

        for i in range(phrase_words):
            if model_predictions[i] == original_errors[i]:
                phrase_correct_original += 1
            if retouched_errors[i] == original_errors[i]:
                phrase_correct_retouched += 1

        return phrase_correct_original, phrase_correct_retouched

    def _update_totals(
        self, *, phrase_words: int, phrase_correct_original: int, phrase_correct_retouched: int
    ) -> None:
        """Update running totals."""
        self.total_words += phrase_words
        self.total_correct_original += phrase_correct_original
        self.total_correct_retouched += phrase_correct_retouched

    def _write_debug_header(self, *, f: Any) -> None:
        """Write debug file header."""
        HEADER_WIDTH: Final[int] = 120
        f.write("=" * HEADER_WIDTH + "\n")
        f.write(f"ACTIVITY DEBUG REPORT: {self.activity.activity_id}\n")
        f.write("=" * HEADER_WIDTH + "\n\n")

    def _write_debug_summary(self, *, f: Any) -> None:
        """Write debug summary section."""
        stats = self.get_activity_accuracy_stats()

        f.write("SUMMARY:\n")
        f.write(f"  Total Words: {stats.total_words}\n")
        f.write(
            f"  Original Accuracy (Model vs Ground Truth): {stats.original_accuracy:.3f} "
            f"({self.total_correct_original}/{stats.total_words})\n"
        )
        f.write(
            f"  Retouched Accuracy (Retouched vs Ground Truth): {stats.retouched_accuracy:.3f} "
            f"({self.total_correct_retouched}/{stats.total_words})\n"
        )
        f.write(f"  Accuracy Improvement: {stats.improvement:+.3f}\n\n")

    def _write_phrase_analysis(self, *, f: Any) -> None:
        """Write phrase-by-phrase analysis section."""
        HEADER_WIDTH: Final[int] = 120
        f.write("PHRASE-BY-PHRASE ANALYSIS:\n")
        f.write("=" * HEADER_WIDTH + "\n\n")

        for debug_info in self.phrase_debug_data:
            self._write_phrase_debug(f=f, debug_info=debug_info)

    def _write_phrase_debug(self, *, f: Any, debug_info: PhraseDebugInfo) -> None:
        """Write debug information for a single phrase."""
        self._write_phrase_header(f=f, debug_info=debug_info)
        self._write_phrase_table(f=f, debug_info=debug_info)
        self._write_phrase_summary(f=f, debug_info=debug_info)

    def _write_phrase_header(self, *, f: Any, debug_info: PhraseDebugInfo) -> None:
        """Write phrase header information."""
        f.write(f"Phrase {debug_info.phrase_index}:\n")
        f.write(f"  Story: '{debug_info.story_phrase}'\n")
        f.write(f"  Decision: {debug_info.decision.reason}\n")
        f.write(f"  Strategy: {debug_info.decision.strategy}\n")

        if debug_info.kaldi_transcript is not None:
            f.write(f"  Kaldi Transcript: '{debug_info.kaldi_transcript}'\n")
        if debug_info.w2v_transcript is not None:
            f.write(f"  W2V Transcript: '{debug_info.w2v_transcript}'\n")
        f.write("\n")

    def _write_phrase_table(self, *, f: Any, debug_info: PhraseDebugInfo) -> None:
        """Write phrase analysis table."""
        COL_WIDTH: Final[int] = 18
        was_rescored = debug_info.decision.strategy == FlaggingStrategy.RESCORE_PHRASE

        headers = self._get_table_headers(was_rescored=was_rescored)

        f.write("  " + "".join(f"{h:<{COL_WIDTH}}" for h in headers) + "\n")
        f.write("  " + "-" * (COL_WIDTH * len(headers)) + "\n")

        for i in range(debug_info.phrase_words):
            row_data = self._get_table_row_data(
                debug_info=debug_info, word_index=i, was_rescored=was_rescored
            )
            f.write("  " + "".join(f"{data!s:<{COL_WIDTH}}" for data in row_data) + "\n")

        f.write("  " + "-" * (COL_WIDTH * len(headers)) + "\n")

    def _write_phrase_summary(self, *, f: Any, debug_info: PhraseDebugInfo) -> None:
        """Write phrase summary statistics."""
        phrase_words = debug_info.phrase_words
        phrase_original_acc = (
            debug_info.phrase_correct_model / phrase_words if phrase_words > 0 else 0
        )
        phrase_retouched_acc = (
            debug_info.phrase_correct_retouched / phrase_words if phrase_words > 0 else 0
        )

        f.write(
            f"  Phrase Summary: Original Accuracy: {phrase_original_acc:.3f} "
            f"({debug_info.phrase_correct_model}/{phrase_words}), "
        )
        f.write(
            f"Retouched Accuracy: {phrase_retouched_acc:.3f} "
            f"({debug_info.phrase_correct_retouched}/{phrase_words})\n\n"
        )

    def _get_table_headers(self, *, was_rescored: bool) -> list[str]:
        """Get table headers based on rescoring status."""
        if was_rescored:
            return [
                "Word",
                "Deepgram",
                "Kaldi→New",
                "W2V→New",
                "Model Pred",
                "Retouch Error",
                "Orig Error",
                "Model Correct",
                "Retouch Correct",
            ]

        return [
            "Word",
            "Deepgram",
            "Kaldi",
            "W2V",
            "Model Pred",
            "Retouch Error",
            "Orig Error",
            "Model Correct",
            "Retouch Correct",
        ]

    def _get_table_row_data(
        self, *, debug_info: PhraseDebugInfo, word_index: int, was_rescored: bool
    ) -> list[str | int | bool]:
        """Get table row data for a specific word."""
        word = (
            debug_info.story_words[word_index]
            if word_index < len(debug_info.story_words)
            else "<?>"
        )
        model_pred = (
            debug_info.model_predictions[word_index]
            if word_index < len(debug_info.model_predictions)
            else True
        )
        deepgram_match = (
            debug_info.deepgram_matches[word_index]
            if word_index < len(debug_info.deepgram_matches)
            else 0
        )

        orig_error = debug_info.original_errors[word_index]
        new_error = debug_info.retouched_errors[word_index]

        original_correct = model_pred == orig_error
        retouched_correct = new_error == orig_error

        if was_rescored and debug_info.rescored_kaldi_matches is not None:
            kaldi_display = self._format_rescored_match(
                original_matches=debug_info.original_kaldi_matches,
                rescored_matches=debug_info.rescored_kaldi_matches,
                word_index=word_index,
            )
            w2v_display = self._format_rescored_match(
                original_matches=debug_info.original_w2v_matches,
                rescored_matches=debug_info.rescored_w2v_matches,
                word_index=word_index,
            )
            return [
                word,
                deepgram_match,
                kaldi_display,
                w2v_display,
                model_pred,
                new_error,
                orig_error,
                original_correct,
                retouched_correct,
            ]

        kaldi_display = str(
            debug_info.original_kaldi_matches[word_index]
            if word_index < len(debug_info.original_kaldi_matches)
            else 0
        )
        w2v_display = str(
            debug_info.original_w2v_matches[word_index]
            if word_index < len(debug_info.original_w2v_matches)
            else 0
        )

        return [
            word,
            deepgram_match,
            kaldi_display,
            w2v_display,
            model_pred,
            new_error,
            orig_error,
            original_correct,
            retouched_correct,
        ]

    def _format_rescored_match(
        self, *, original_matches: list[int], rescored_matches: list[int] | None, word_index: int
    ) -> str:
        """Format rescored match display."""
        orig = original_matches[word_index] if word_index < len(original_matches) else 0
        new = (
            rescored_matches[word_index]
            if rescored_matches and word_index < len(rescored_matches)
            else 0
        )
        return f"{orig}→{new}"
