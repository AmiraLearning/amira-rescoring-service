"""
Test suite for file operations utility functions.

This module tests utility functions in the file_operations module,
particularly the trim_predictions function which handles trimming
trailing errors from prediction arrays.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.orf_rescoring_pipeline.utils.file_operations import trim_predictions

PredictionMatrix = list[list[bool]]


@pytest.mark.unit
class TestTrimPredictions:
    """Test cases for trim_predictions function."""

    def test_trim_trailing_errors_simple_case(self) -> None:
        """Test basic trimming of trailing errors in last phrase."""
        predictions: PredictionMatrix = [
            [False, True, False],  # phrase 1: correct, error, correct
            [True, False, True, True],  # phrase 2: error, correct, error, error
        ]
        expected = [
            [False, True, False],  # phrase 1: unchanged
            [True, False],  # phrase 2: trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_trim_all_trailing_errors_multiple_phrases(self) -> None:
        """Test trimming trailing errors across multiple phrases."""
        predictions: PredictionMatrix = [
            [False, False],  # phrase 1: correct, correct
            [True, False],  # phrase 2: error, correct
            [True, True, True],  # phrase 3: all errors (should be removed)
        ]
        expected = [
            [False, False],  # phrase 1: unchanged
            [True, False],  # phrase 2: unchanged, phrase 3 removed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_no_trimming_needed_no_trailing_errors(self) -> None:
        """Test that predictions with no trailing errors are unchanged."""
        predictions: PredictionMatrix = [
            [False, True, False],  # phrase 1: ends with correct
            [True, False],  # phrase 2: ends with correct
        ]
        expected = [[False, True, False], [True, False]]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_word_phrases_not_trimmed(self) -> None:
        """Test that single-word phrases are never trimmed."""
        predictions: PredictionMatrix = [
            [False],  # single correct word
            [True],  # single error word
            [False, True, True],  # multi-word phrase with trailing errors
        ]
        expected = [
            [False],  # unchanged (single word)
            [True],  # unchanged (single word)
            [False],  # trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_all_errors_in_predictions(self) -> None:
        """Test behavior when all predictions are errors."""
        predictions: PredictionMatrix = [
            [True, True],  # all errors
            [True, True, True],  # all errors
        ]
        # When all predictions are errors, function catches exception and returns original
        expected = [[True, True], [True, True, True]]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_with_trailing_errors(self) -> None:
        """Test trimming when there's only one phrase with trailing errors."""
        predictions: PredictionMatrix = [
            [False, True, False, True, True]  # correct, error, correct, error, error
        ]
        expected = [
            [False, True, False]  # trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_all_errors_except_first(self) -> None:
        """Test single phrase where only first word is correct."""
        predictions: PredictionMatrix = [
            [False, True, True, True]  # correct, error, error, error
        ]
        expected = [
            [False]  # only the correct word remains
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_single_word_error(self) -> None:
        """Test single phrase with single error word (should not be trimmed)."""
        predictions: PredictionMatrix = [
            [True]  # single error word
        ]
        expected = [
            [True]  # unchanged due to single word rule
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_single_word_correct(self) -> None:
        """Test single phrase with single correct word."""
        predictions: PredictionMatrix = [
            [False]  # single correct word
        ]
        expected = [
            [False]  # unchanged
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_empty_predictions_list(self) -> None:
        """Test behavior with empty predictions list."""
        predictions: PredictionMatrix = []
        # Should handle gracefully and return original
        result = trim_predictions(predictions=predictions)
        assert result == predictions

    def test_phrase_becomes_empty_after_trimming(self) -> None:
        """Test phrases that become empty after trimming are removed."""
        predictions: PredictionMatrix = [
            [False, True],  # correct, error
            [True, True],  # all errors (should be removed)
        ]
        # The function trims to the last non-error, which is the False in first phrase
        expected = [
            [False]  # trimmed to last non-error
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_complex_multi_phrase_scenario(self) -> None:
        """Test complex scenario with multiple phrases and various patterns."""
        predictions: PredictionMatrix = [
            [False, False, True],  # phrase 1: correct, correct, error
            [True, False, True],  # phrase 2: error, correct, error
            [False, True, False],  # phrase 3: correct, error, correct
            [True, True, True, True],  # phrase 4: all errors
        ]
        expected = [
            [False, False, True],  # phrase 1: unchanged
            [True, False, True],  # phrase 2: unchanged
            [False, True, False],  # phrase 3: unchanged, phrase 4 removed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_only_last_phrase_has_trailing_errors(self) -> None:
        """Test when only the last phrase needs trimming."""
        predictions: PredictionMatrix = [
            [False, True, False],  # phrase 1: ends correctly
            [True, False, True],  # phrase 2: ends with error
            [False, False, True, True, True],  # phrase 3: has trailing errors
        ]
        expected = [
            [False, True, False],  # phrase 1: unchanged
            [True, False, True],  # phrase 2: unchanged
            [False, False],  # phrase 3: trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    @patch("src.orf_rescoring_pipeline.utils.file_operations.logger")
    def test_exception_handling_returns_original(self, mock_logger: MagicMock) -> None:
        """Test that exceptions are caught and original predictions returned."""
        # Test the actual exception case we saw - all errors scenario
        predictions: PredictionMatrix = [
            [True, True],  # all errors
            [True, True, True],  # all errors
        ]

        # This should trigger the exception handling path and return original
        result = trim_predictions(predictions=predictions)

        # Should return original predictions when exception occurs
        assert result == predictions

        # Verify that a warning was logged
        mock_logger.warning.assert_called_once()

    def test_edge_case_last_phrase_only_errors(self) -> None:
        """Test when last phrase contains only errors."""
        predictions: PredictionMatrix = [
            [False, True, False],  # phrase 1: mixed
            [True, True],  # phrase 2: only errors
        ]
        expected = [
            [False, True, False]  # phrase 1: unchanged, phrase 2 removed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_alternating_pattern_with_trailing_errors(self) -> None:
        """Test alternating correct/error pattern with trailing errors."""
        predictions: PredictionMatrix = [
            [False, True, False, True, False],  # alternating, ends correct
            [True, False, True, True, True],  # alternating then trailing errors
        ]
        expected = [
            [False, True, False, True, False],  # unchanged
            [True, False],  # trimmed to last correct
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_nested_empty_phrases_handling(self) -> None:
        """Test handling of edge cases with phrase structure."""
        predictions: PredictionMatrix = [
            [False],  # single correct
            [True, False],  # error then correct
            [True, True, True],  # all errors
        ]
        expected = [
            [False],  # unchanged (single word)
            [True, False],  # unchanged (ends with correct)
            # third phrase removed (all errors)
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected
