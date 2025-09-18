"""
Test suite for flagging rules functionality.

This module tests the flagging system that analyzes features and decides on
flagging strategies for phrase rescoring based on ASR match data and model predictions.
"""

from typing import TypedDict, cast
from unittest.mock import Mock, patch

import pytest

from src.orf_rescoring_pipeline import constants
from src.orf_rescoring_pipeline.models import Activity, ModelFeature
from src.orf_rescoring_pipeline.rules.flagging import (
    FlaggingAnalyzer,
    FlaggingExecutor,
)
from src.orf_rescoring_pipeline.utils.debug import (
    FlaggingDecision,
    FlaggingStrategy,
)


class EdgeCaseDict(TypedDict):
    """Type definition for edge case test data."""

    name: str
    deepgram_match: list[int]
    model_preds: list[bool]
    w2v_match: list[int]


class ScenarioDict(TypedDict):
    """Type definition for scenario test data."""

    name: str
    deepgram_match: list[int]
    model_preds: list[bool]
    w2v_match: list[int]
    expected_strategy: FlaggingStrategy


@pytest.mark.unit
class TestFlaggingAnalyzer:
    """Test cases for FlaggingAnalyzer class."""

    def test_edm_correct_threshold_value(self: "TestFlaggingAnalyzer") -> None:
        """Test that EDM_CORRECT_THRESHOLD has expected value - will fail if changed."""
        assert constants.EDM_CORRECT_THRESHOLD == 2, (
            f"EDM_CORRECT_THRESHOLD changed from 2 to {constants.EDM_CORRECT_THRESHOLD}. "
            "Update flagging tests if this change is intentional."
        )

    def test_count_deepgram_matches(self: "TestFlaggingAnalyzer") -> None:
        """Test counting Deepgram matches."""
        feature = ModelFeature(
            model="test_model", phrase_index=0, deepgram_match=[1, 1, 0, 1, 0, 1]
        )

        count = FlaggingAnalyzer.count_deepgram_matches(feature=feature)
        assert count == 4, "Should count 4 matches (1s) out of 6 total"

    def test_count_deepgram_matches_empty(self: "TestFlaggingAnalyzer") -> None:
        """Test counting Deepgram matches with empty list."""
        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[])

        count = FlaggingAnalyzer.count_deepgram_matches(feature=feature)
        assert count == 0, "Empty list should return 0 matches"

    def test_count_deepgram_matches_all_zeros(self: "TestFlaggingAnalyzer") -> None:
        """Test counting Deepgram matches with all zeros."""
        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[0, 0, 0, 0])

        count = FlaggingAnalyzer.count_deepgram_matches(feature=feature)
        assert count == 0, "All zeros should return 0 matches"

    def test_count_edm_corrects(self: "TestFlaggingAnalyzer") -> None:
        """Test counting EDM correct predictions."""
        # EDM predictions: False = correct, True = miscue
        model_preds = [False, True, False, True, False]

        count = FlaggingAnalyzer.count_edm_corrects(errors=model_preds)
        assert count == 3, "Should count 3 correct predictions (False values)"

    def test_count_edm_corrects_all_miscues(self: "TestFlaggingAnalyzer") -> None:
        """Test counting EDM corrects when all are miscues."""
        model_preds = [True, True, True, True]

        count = FlaggingAnalyzer.count_edm_corrects(errors=model_preds)
        assert count == 0, "All miscues should return 0 corrects"

    def test_count_edm_corrects_all_correct(self: "TestFlaggingAnalyzer") -> None:
        """Test counting EDM corrects when all are correct."""
        model_preds = [False, False, False]

        count = FlaggingAnalyzer.count_edm_corrects(errors=model_preds)
        assert count == 3, "All correct should return count equal to length"

    def test_count_w2v_matches(self: "TestFlaggingAnalyzer") -> None:
        """Test counting W2V matches."""
        feature = ModelFeature(model="test_model", phrase_index=0, w2v_match=[1, 0, 1, 1, 0])

        count = FlaggingAnalyzer.count_w2v_matches(feature=feature)
        assert count == 3, "Should count 3 matches (1s) out of 5 total"

    def test_deepgram_edm_agree_perfect_agreement(self: "TestFlaggingAnalyzer") -> None:
        """Test agreement when Deepgram and EDM perfectly agree."""
        deepgram_matches = [1, 1, 0, 0]
        edm_preds = [False, False, True, True]  # False=correct, True=miscue

        # Agreement means: deepgram_match=1 corresponds to edm_pred=False (correct)
        # and deepgram_match=0 corresponds to edm_pred=True (miscue)
        agrees = FlaggingAnalyzer.deepgram_edm_agree(
            deepgram_matches=deepgram_matches, edm_preds=edm_preds
        )
        assert agrees is True, "Perfect agreement should return True"

    def test_deepgram_edm_agree_disagreement(self: "TestFlaggingAnalyzer") -> None:
        """Test agreement when Deepgram and EDM disagree."""
        deepgram_matches = [1, 0, 1, 0]
        edm_preds = [True, False, False, True]  # Disagreement pattern

        agrees = FlaggingAnalyzer.deepgram_edm_agree(
            deepgram_matches=deepgram_matches, edm_preds=edm_preds
        )
        assert agrees is False, "Disagreement should return False"

    def test_deepgram_edm_agree_partial_agreement(self: "TestFlaggingAnalyzer") -> None:
        """Test agreement when Deepgram and EDM partially agree."""
        deepgram_matches = [1, 1, 0]
        edm_preds = [False, True, True]  # First agrees, second and third disagree

        agrees = FlaggingAnalyzer.deepgram_edm_agree(
            deepgram_matches=deepgram_matches, edm_preds=edm_preds
        )
        assert agrees is False, "Partial agreement should return False"

    def test_decide_flagging_strategy_deepgram_no_words_edm_no_corrects(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging decision when Deepgram found no words and EDM found no corrects."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[0, 0, 0, 0],
            w2v_match=[0, 0, 0, 0],
        )
        model_preds = [True, True, True, True]  # All miscues (no corrects)

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.KEEP_ORIGINAL
        assert "Deepgram found no words and EDM found all miscues" in decision.reason

    def test_decide_flagging_strategy_deepgram_no_words_edm_many_corrects(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging decision when Deepgram found no words but EDM found many corrects."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[0, 0, 0, 0],
            w2v_match=[0, 0, 0, 0],
        )
        model_preds = [
            False,
            False,
            False,
            True,
        ]  # 3 corrects > EDM_CORRECT_THRESHOLD (2)

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.RESCORE_PHRASE
        assert "EDM found 3 corrects (>2)" in decision.reason

    def test_decide_flagging_strategy_deepgram_no_words_edm_few_corrects_w2v_no_matches(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging when Deepgram has no words, EDM has few corrects, W2V has no matches."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[0, 0, 0, 0],
            w2v_match=[0, 0, 0, 0],
        )
        model_preds = [False, True, True, True]  # 1 correct <= threshold of 2

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.MARK_ALL_ERRORS
        assert "mark all as errors" in decision.reason

    def test_decide_flagging_strategy_deepgram_no_words_edm_few_corrects_w2v_has_matches(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging when Deepgram has no words, EDM has few corrects, but W2V has matches."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[0, 0, 0, 0],
            w2v_match=[1, 0, 1, 0],  # 2 matches
        )
        model_preds = [False, True, True, True]  # 1 correct <= threshold of 2

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.RESCORE_PHRASE
        assert "W2V found 2 matches - rescore needed" in decision.reason

    def test_decide_flagging_strategy_deepgram_has_words_agrees_with_edm(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging when Deepgram found words and agrees with EDM."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 1, 0, 0],
            w2v_match=[1, 1, 0, 0],
        )
        model_preds = [False, False, True, True]  # Agreement pattern

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.KEEP_ORIGINAL
        assert "agrees with EDM" in decision.reason

    def test_decide_flagging_strategy_deepgram_has_words_disagrees_with_edm(
        self: "TestFlaggingAnalyzer",
    ) -> None:
        """Test flagging when Deepgram found words but disagrees with EDM."""
        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0, 1, 0],
            w2v_match=[1, 0, 1, 0],
        )
        model_preds = [True, False, False, True]  # Disagreement pattern

        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        assert decision.strategy == FlaggingStrategy.RESCORE_PHRASE
        assert "disagrees with EDM" in decision.reason


@pytest.mark.unit
class TestFlaggingExecutor:
    """Test cases for FlaggingExecutor class."""

    def test_init(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test FlaggingExecutor initialization."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        assert executor.activity == sample_activity
        assert executor.kaldi == mock_kaldi_client
        assert executor.w2v == mock_w2v_client
        assert executor.debugger is None

    def test_init_with_debugger(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test FlaggingExecutor initialization with debugger."""
        mock_debugger = Mock()
        executor = FlaggingExecutor(
            activity=sample_activity,
            kaldi=mock_kaldi_client,
            w2v=mock_w2v_client,
            debugger=mock_debugger,
        )

        assert executor.debugger == mock_debugger

    def test_execute_flagging_decision_keep_original(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test executing KEEP_ORIGINAL flagging decision."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 1, 0, 1, 0])

        decision = FlaggingDecision(
            FlaggingStrategy.KEEP_ORIGINAL, "Test reason for keeping original"
        )

        # Setup activity with existing errors
        sample_activity.errors = [[True, False, True, False, True]]

        result = executor.execute_flagging_decision(feature=feature, decision=decision)

        # Should return the original errors
        assert result == [True, False, True, False, True]

    def test_execute_flagging_decision_mark_all_errors(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test executing MARK_ALL_ERRORS flagging decision."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[0, 0, 0])

        decision = FlaggingDecision(
            FlaggingStrategy.MARK_ALL_ERRORS, "Test reason for marking all errors"
        )

        # Setup activity with model predictions - mock the property
        with patch.object(
            type(sample_activity),
            "model_predictions",
            new_callable=lambda: [[False, True, False]],
        ):
            result = executor.execute_flagging_decision(feature=feature, decision=decision)

            # Should return all True (all errors)
            assert result == [True, True, True]

    @patch("src.orf_rescoring_pipeline.rules.rescoring.rescore_phrase")
    def test_execute_flagging_decision_rescore_phrase(
        self: "TestFlaggingExecutor",
        mock_rescore: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test executing RESCORE_PHRASE flagging decision."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 0, 1])

        decision = FlaggingDecision(FlaggingStrategy.RESCORE_PHRASE, "Test reason for rescoring")

        # Mock the rescore_phrase function
        mock_rescore.return_value = (
            [False, True, False],  # retouched_errors
            [1, 0, 1],  # kaldi_matches
            [1, 1, 0],  # w2v_matches
            "hello world test",  # kaldi_transcript
            "hello world test",  # w2v_transcript
        )

        result = executor.execute_flagging_decision(feature=feature, decision=decision)

        # Should return the rescored errors
        assert result == [False, True, False]

        # Should call rescore_phrase with correct parameters
        mock_rescore.assert_called_once_with(
            activity=sample_activity, feature=feature, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        # Should store the match results
        assert feature.resliced_kaldi_match == [1, 0, 1]
        assert feature.resliced_w2v_match == [1, 1, 0]

    def test_execute_flagging_decision_invalid_strategy(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test executing invalid flagging decision raises error."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 0, 1])

        # Create decision with invalid strategy - bypassing type checking for test
        decision = FlaggingDecision(cast(FlaggingStrategy, "INVALID_STRATEGY"), "Invalid strategy")

        with pytest.raises(ValueError, match="Unknown flagging strategy"):
            executor.execute_flagging_decision(feature=feature, decision=decision)

    def test_execute_flagging_decision_with_debugger(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test executing flagging decision with debugger enabled."""
        mock_debugger = Mock()
        executor = FlaggingExecutor(
            activity=sample_activity,
            kaldi=mock_kaldi_client,
            w2v=mock_w2v_client,
            debugger=mock_debugger,
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 1, 0])

        decision = FlaggingDecision(FlaggingStrategy.KEEP_ORIGINAL, "Test reason")

        # Setup activity
        sample_activity.errors = [[True, False, True]]
        sample_activity.phrases = ["hello world test"]

        executor.execute_flagging_decision(feature=feature, decision=decision)

        # Should call debugger
        mock_debugger.add_phrase_debug_info.assert_called_once()

        # Verify debugger was called with correct parameters
        call_kwargs = mock_debugger.add_phrase_debug_info.call_args.kwargs
        assert call_kwargs["feature"] == feature
        assert call_kwargs["decision"] == decision
        assert call_kwargs["original_errors"] == [True, False, True]
        assert call_kwargs["retouched_errors"] == [True, False, True]
        assert call_kwargs["story_phrase"] == "hello world test"
        assert call_kwargs["kaldi_transcript"] is None  # None for KEEP_ORIGINAL
        assert call_kwargs["w2v_transcript"] is None  # None for KEEP_ORIGINAL

    def test_get_original_errors_exists(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test getting original errors when they exist."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 0, 1])

        # Setup activity with errors
        sample_activity.errors = [[True, False, True], [False, True, False]]

        original_errors = executor._get_original_errors(feature=feature)

        assert original_errors == [True, False, True]

    def test_get_original_errors_fallback(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test getting original errors when they don't exist (fallback)."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 0, 1])

        # Setup activity without errors but with model predictions
        sample_activity.errors = []  # Empty errors
        with patch.object(
            type(sample_activity),
            "model_predictions",
            new_callable=lambda: [[False, True, False]],
        ):
            original_errors = executor._get_original_errors(feature=feature)

            # Should fallback to all True
            assert original_errors == [True, True, True]

    def test_keep_original_errors_phrase_exists(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test keeping original errors when phrase exists."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(
            model="test_model",
            phrase_index=1,  # Second phrase
            deepgram_match=[1, 0],
        )

        sample_activity.errors = [[True, False], [False, True]]

        result = executor._keep_original_errors(feature=feature)

        assert result == [False, True]  # Second phrase errors

    def test_mark_all_as_errors(
        self: "TestFlaggingExecutor",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test marking all words as errors."""
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        feature = ModelFeature(model="test_model", phrase_index=0, deepgram_match=[1, 0, 1, 0])

        with patch.object(
            type(sample_activity),
            "model_predictions",
            new_callable=lambda: [[False, True, False, True]],
        ):
            result = executor._mark_all_as_errors(feature=feature)

            assert result == [True, True, True, True]


@pytest.mark.unit
class TestFlaggingAnalyzerCombined:
    """Integration tests for flagging functionality."""

    def test_complete_flagging_workflow(
        self: "TestFlaggingAnalyzerCombined",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test complete flagging workflow from analysis to execution."""
        # Setup test data
        feature = ModelFeature(
            model="test_model_v1",
            phrase_index=0,
            deepgram_match=[1, 0, 1, 0],
            w2v_match=[1, 1, 0, 1],
            kaldi_match=[0, 1, 1, 1],
        )

        model_preds = [
            True,
            False,
            True,
            False,
        ]  # 2 corrects = EDM_CORRECT_THRESHOLD, disagrees with Deepgram

        sample_activity.errors = [[True, False, True, False]]
        sample_activity.phrases = ["hello world test phrase"]

        # Step 1: Analyze and decide strategy
        decision = FlaggingAnalyzer.decide_flagging_strategy(feature=feature, errors=model_preds)

        # Should decide to rescore because Deepgram found words but disagrees with EDM
        assert decision.strategy == FlaggingStrategy.RESCORE_PHRASE

        # Step 2: Execute the decision
        executor = FlaggingExecutor(
            activity=sample_activity, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        with patch("src.orf_rescoring_pipeline.rules.rescoring.rescore_phrase") as mock_rescore:
            mock_rescore.return_value = (
                [False, False, True, False],  # New error array
                [1, 1, 0, 1],  # Kaldi matches
                [1, 1, 1, 0],  # W2V matches
                "hello world phrase",  # Kaldi transcript
                "hello world phrase",  # W2V transcript
            )

            retouched_errors = executor.execute_flagging_decision(
                feature=feature, decision=decision
            )

            assert retouched_errors == [False, False, True, False]
            mock_rescore.assert_called_once()

    def test_multiple_scenarios_decision_making(self: "TestFlaggingAnalyzerCombined") -> None:
        """Test decision making across multiple realistic scenarios."""
        scenarios: list[ScenarioDict] = [
            {
                "name": "Perfect Deepgram, EDM agrees",
                "deepgram_match": [1, 1, 1, 1],
                "model_preds": [False, False, False, False],  # All correct, agrees
                "w2v_match": [1, 1, 1, 1],
                "expected_strategy": FlaggingStrategy.KEEP_ORIGINAL,
            },
            {
                "name": "No Deepgram matches, no EDM corrects",
                "deepgram_match": [0, 0, 0],
                "model_preds": [True, True, True],  # All miscues
                "w2v_match": [0, 0, 0],
                "expected_strategy": FlaggingStrategy.KEEP_ORIGINAL,
            },
            {
                "name": "No Deepgram, few EDM corrects, no W2V",
                "deepgram_match": [0, 0, 0, 0],
                "model_preds": [False, True, True, True],  # 1 correct
                "w2v_match": [0, 0, 0, 0],
                "expected_strategy": FlaggingStrategy.MARK_ALL_ERRORS,
            },
            {
                "name": "Deepgram found words but disagrees with EDM",
                "deepgram_match": [1, 1, 0],
                "model_preds": [True, True, False],  # Disagrees
                "w2v_match": [1, 0, 1],
                "expected_strategy": FlaggingStrategy.RESCORE_PHRASE,
            },
        ]

        # Using actual EDM_CORRECT_THRESHOLD value to catch logic changes
        for scenario in scenarios:
            deepgram_match: list[int] = scenario["deepgram_match"]
            w2v_match: list[int] = scenario["w2v_match"]
            model_preds: list[bool] = scenario["model_preds"]
            expected_strategy: FlaggingStrategy = scenario["expected_strategy"]

            feature = ModelFeature(
                model="test_model",
                phrase_index=0,
                deepgram_match=deepgram_match,
                w2v_match=w2v_match,
            )

            decision = FlaggingAnalyzer.decide_flagging_strategy(
                feature=feature, errors=model_preds
            )

            assert decision.strategy == expected_strategy, (
                f"Scenario '{scenario['name']}' failed: expected {expected_strategy}, "
                f"got {decision.strategy}"
            )

    def test_edge_cases_handling(
        self: "TestFlaggingAnalyzerCombined",
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test handling of edge cases in flagging."""
        edge_cases: list[EdgeCaseDict] = [
            {
                "name": "Empty matches and predictions",
                "deepgram_match": [],
                "model_preds": [],
                "w2v_match": [],
            },
            {
                "name": "Single word phrase",
                "deepgram_match": [1],
                "model_preds": [False],
                "w2v_match": [1],
            },
            {
                "name": "Very long phrase",
                "deepgram_match": [1, 0, 1, 0, 1] * 10,  # 50 elements
                "model_preds": [True, False] * 25,  # 50 elements
                "w2v_match": [0, 1, 0, 1, 0] * 10,  # 50 elements
            },
        ]

        for case in edge_cases:
            deepgram_match: list[int] = case["deepgram_match"]
            w2v_match: list[int] = case["w2v_match"]
            model_preds: list[bool] = case["model_preds"]

            feature = ModelFeature(
                model="test_model",
                phrase_index=0,
                deepgram_match=deepgram_match,
                w2v_match=w2v_match,
            )

            # Should not raise exceptions
            try:
                decision = FlaggingAnalyzer.decide_flagging_strategy(
                    feature=feature, errors=model_preds
                )

                # Basic validation
                assert hasattr(decision.strategy, "value") or isinstance(decision.strategy, str)
                assert isinstance(decision.reason, str)
                assert len(decision.reason) > 0

            except Exception as e:
                pytest.fail(f"Edge case '{case['name']}' raised exception: {e}")

            # Should not reach here if exception was raised
