"""
Flagging system for ORF rescoring pipeline.

This module contains the logic for analyzing features and deciding on flagging strategies
for phrase rescoring based on ASR match data and model predictions.
"""

import logging

from orf_rescoring_pipeline import constants
from orf_rescoring_pipeline.models import Activity, ModelFeature
from orf_rescoring_pipeline.utils.debug import (
    ActivityDebugger,
    FlaggingDecision,
    FlaggingStrategy,
)
from orf_rescoring_pipeline.utils.transcription import KaldiASRClient, W2VASRClient

logger = logging.getLogger(__name__)


class FlaggingAnalyzer:
    """Analyzes features to determine the appropriate flagging strategy."""

    @staticmethod
    def count_deepgram_matches(*, feature: ModelFeature) -> int:
        """Count number of words matched by Deepgram.

        Args:
            feature: ModelFeature containing Deepgram match data.

        Returns:
            Number of words matched by Deepgram.
        """
        return sum(feature.deepgram_match)

    @staticmethod
    def count_edm_corrects(*, errors: list[bool]) -> int:
        """Count number of correct predictions by EDM.

        Args:
            errors: List of error flags where False indicates correct prediction.

        Returns:
            Number of correct predictions (False values in errors list).
        """
        return errors.count(False)

    @staticmethod
    def count_w2v_matches(*, feature: ModelFeature) -> int:
        """Count number of words matched by W2V.

        Args:
            feature: ModelFeature containing W2V match data.

        Returns:
            Number of words matched by W2V.
        """
        return sum(feature.w2v_match)

    @staticmethod
    def deepgram_edm_agree(*, deepgram_matches: list[int], edm_preds: list[bool]) -> bool:
        """Check if Deepgram and EDM predictions agree.

        Args:
            deepgram_matches: List of Deepgram match indicators.
            edm_preds: List of EDM error predictions.

        Returns:
            True if Deepgram and EDM predictions agree for all words.
        """
        return all(
            bool(deepgram_match) != edm_pred
            for deepgram_match, edm_pred in zip(deepgram_matches, edm_preds)
        )

    @classmethod
    def decide_flagging_strategy(
        cls, *, feature: ModelFeature, errors: list[bool]
    ) -> FlaggingDecision:
        """Decide the flagging strategy based on feature analysis.

        Args:
            feature: ModelFeature containing ASR match data.
            errors: EDM errors array where False indicates correct prediction.

        Returns:
            FlaggingDecision with strategy and reasoning.
        """
        deepgram_matches = cls.count_deepgram_matches(feature=feature)
        edm_corrects = cls.count_edm_corrects(errors=errors)
        w2v_matches = cls.count_w2v_matches(feature=feature)

        if deepgram_matches == 0:
            return cls._handle_no_deepgram_matches(
                edm_corrects=edm_corrects, w2v_matches=w2v_matches
            )
        return cls._handle_deepgram_matches(
            feature=feature, errors=errors, deepgram_matches=deepgram_matches
        )

    @classmethod
    def _handle_no_deepgram_matches(
        cls, *, edm_corrects: int, w2v_matches: int
    ) -> FlaggingDecision:
        """Handle case where Deepgram found no words.

        Args:
            edm_corrects: Number of correct EDM predictions.
            w2v_matches: Number of W2V matches.

        Returns:
            FlaggingDecision for no Deepgram matches scenario.
        """
        match edm_corrects:
            case 0:
                return FlaggingDecision(
                    FlaggingStrategy.KEEP_ORIGINAL,
                    "Deepgram found no words and EDM found all miscues - keep original",
                )
            case _ if edm_corrects > constants.EDM_CORRECT_THRESHOLD:
                return FlaggingDecision(
                    FlaggingStrategy.RESCORE_PHRASE,
                    f"Deepgram found no words but EDM found {edm_corrects} corrects (>{constants.EDM_CORRECT_THRESHOLD}) - rescore needed",
                )
            case _:
                if w2v_matches == 0:
                    return FlaggingDecision(
                        FlaggingStrategy.MARK_ALL_ERRORS,
                        f"Deepgram found no words, EDM found {edm_corrects} corrects (<={constants.EDM_CORRECT_THRESHOLD}), W2V found no matches - mark all as errors",
                    )
                return FlaggingDecision(
                    FlaggingStrategy.RESCORE_PHRASE,
                    f"Deepgram found no words, EDM found {edm_corrects} corrects (<={constants.EDM_CORRECT_THRESHOLD}), but W2V found {w2v_matches} matches - rescore needed",
                )

    @classmethod
    def _handle_deepgram_matches(
        cls, *, feature: ModelFeature, errors: list[bool], deepgram_matches: int
    ) -> FlaggingDecision:
        """Handle case where Deepgram found some words.

        Args:
            feature: ModelFeature containing match data.
            errors: List of EDM error predictions.
            deepgram_matches: Number of Deepgram matches.

        Returns:
            FlaggingDecision for Deepgram matches scenario.
        """
        if cls.deepgram_edm_agree(deepgram_matches=feature.deepgram_match, edm_preds=errors):
            return FlaggingDecision(
                FlaggingStrategy.KEEP_ORIGINAL,
                f"Deepgram found {deepgram_matches} words and agrees with EDM - keep original",
            )
        return FlaggingDecision(
            FlaggingStrategy.RESCORE_PHRASE,
            f"Deepgram found {deepgram_matches} words but disagrees with EDM - rescore needed",
        )


class FlaggingExecutor:
    """Executes flagging decisions by applying the appropriate strategy."""

    def __init__(
        self,
        *,
        activity: Activity,
        kaldi: KaldiASRClient,
        w2v: W2VASRClient,
        debugger: ActivityDebugger | None = None,
    ) -> None:
        """Initialize FlaggingExecutor.

        Args:
            activity: Activity being processed.
            kaldi: Kaldi ASR client for rescoring.
            w2v: W2V ASR client for rescoring.
            debugger: Optional debugger for collecting debug information.
        """
        self.activity = activity
        self.kaldi = kaldi
        self.w2v = w2v
        self.debugger = debugger

    def execute_flagging_decision(
        self, *, feature: ModelFeature, decision: FlaggingDecision
    ) -> list[bool]:
        """Execute a flagging decision and return the error array.

        Args:
            feature: ModelFeature being processed.
            decision: FlaggingDecision to execute.

        Returns:
            List of error flags for the phrase.
        """
        logger.debug(
            f"Activity {self.activity.activity_id}, phrase {feature.phrase_index}: {decision.reason}"
        )

        original_errors = self._get_original_errors(feature=feature)
        kaldi_transcript = None
        w2v_transcript = None

        match decision.strategy:
            case FlaggingStrategy.KEEP_ORIGINAL:
                retouched_errors = self._keep_original_errors(feature=feature)
            case FlaggingStrategy.MARK_ALL_ERRORS:
                retouched_errors = self._mark_all_as_errors(feature=feature)
            case FlaggingStrategy.RESCORE_PHRASE:
                retouched_errors, kaldi_transcript, w2v_transcript = self._rescore_phrase(
                    feature=feature
                )
            case _:
                raise ValueError(f"Unknown flagging strategy: {decision.strategy}")

        if self.debugger is not None:
            self._collect_debug_info(
                feature=feature,
                decision=decision,
                original_errors=original_errors,
                retouched_errors=retouched_errors,
                kaldi_transcript=kaldi_transcript,
                w2v_transcript=w2v_transcript,
            )

        return retouched_errors

    def _get_original_errors(self, *, feature: ModelFeature) -> list[bool]:
        """Get the original error array for this phrase.

        Args:
            feature: ModelFeature being processed.

        Returns:
            Original error array for the phrase.
        """
        return self._keep_original_errors(feature=feature)

    def _keep_original_errors(self, *, feature: ModelFeature) -> list[bool]:
        """Keep the original error array for this phrase.

        Args:
            feature: ModelFeature being processed.

        Returns:
            Original error array or fallback if not available.
        """
        if feature.phrase_index < len(self.activity.errors):
            return self.activity.errors[feature.phrase_index]
        model_preds = self.activity.model_predictions[feature.phrase_index]
        return [True] * len(model_preds)

    def _mark_all_as_errors(self, *, feature: ModelFeature) -> list[bool]:
        """Mark all words in the phrase as errors.

        Args:
            feature: ModelFeature being processed.

        Returns:
            List with all words marked as errors.
        """
        model_preds = self.activity.model_predictions[feature.phrase_index]
        return [True] * len(model_preds)

    def _rescore_phrase(self, *, feature: ModelFeature) -> tuple[list[bool], str, str]:
        """Rescore the phrase using ASR systems and return transcripts.

        Args:
            feature: ModelFeature being processed.

        Returns:
            Tuple of (retouched_errors, kaldi_transcript, w2v_transcript).
        """
        from orf_rescoring_pipeline.rules.rescoring import rescore_phrase

        (
            retouched_errors,
            kaldi_matches,
            w2v_matches,
            kaldi_transcript,
            w2v_transcript,
        ) = rescore_phrase(activity=self.activity, feature=feature, kaldi=self.kaldi, w2v=self.w2v)

        feature.resliced_kaldi_match = kaldi_matches
        feature.resliced_w2v_match = w2v_matches
        return retouched_errors, kaldi_transcript, w2v_transcript

    def _collect_debug_info(
        self,
        *,
        feature: ModelFeature,
        decision: FlaggingDecision,
        original_errors: list[bool],
        retouched_errors: list[bool],
        kaldi_transcript: str | None,
        w2v_transcript: str | None,
    ) -> None:
        """Collect debug information for the phrase.

        Args:
            feature: ModelFeature being processed.
            decision: FlaggingDecision that was executed.
            original_errors: Original error array.
            retouched_errors: Final error array after processing.
            kaldi_transcript: Kaldi transcript if rescoring occurred.
            w2v_transcript: W2V transcript if rescoring occurred.
        """
        if self.debugger is not None:
            story_phrase = self.activity.phrases[feature.phrase_index]
            self.debugger.add_phrase_debug_info(
                feature=feature,
                decision=decision,
                original_errors=original_errors,
                retouched_errors=retouched_errors,
                story_phrase=story_phrase,
                kaldi_transcript=kaldi_transcript,
                w2v_transcript=w2v_transcript,
            )
