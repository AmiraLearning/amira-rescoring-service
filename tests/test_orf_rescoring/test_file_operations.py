"""
Test suite for file operations utility functions.

This module tests utility functions in the file_operations module,
particularly the trim_predictions function which handles trimming
trailing errors from prediction arrays.
"""

from unittest.mock import patch

import pytest

from src.orf_rescoring_pipeline.utils.file_operations import trim_predictions


@pytest.mark.unit
class TestTrimPredictions:
    """Test cases for trim_predictions function."""

    def test_trim_trailing_errors_simple_case(self):
        """Test basic trimming of trailing errors in last phrase."""
        predictions = [
            [False, True, False],  # phrase 1: correct, error, correct
            [True, False, True, True],  # phrase 2: error, correct, error, error
        ]
        expected = [
            [False, True, False],  # phrase 1: unchanged
            [True, False],  # phrase 2: trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_trim_all_trailing_errors_multiple_phrases(self):
        """Test trimming trailing errors across multiple phrases."""
        predictions = [
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

    def test_no_trimming_needed_no_trailing_errors(self):
        """Test that predictions with no trailing errors are unchanged."""
        predictions = [
            [False, True, False],  # phrase 1: ends with correct
            [True, False],  # phrase 2: ends with correct
        ]
        expected = [[False, True, False], [True, False]]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_word_phrases_not_trimmed(self):
        """Test that single-word phrases are never trimmed."""
        predictions = [
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

    def test_all_errors_in_predictions(self):
        """Test behavior when all predictions are errors."""
        predictions = [
            [True, True],  # all errors
            [True, True, True],  # all errors
        ]
        # When all predictions are errors, function catches exception and returns original
        expected = [[True, True], [True, True, True]]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_with_trailing_errors(self):
        """Test trimming when there's only one phrase with trailing errors."""
        predictions = [
            [False, True, False, True, True]  # correct, error, correct, error, error
        ]
        expected = [
            [False, True, False]  # trailing errors trimmed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_all_errors_except_first(self):
        """Test single phrase where only first word is correct."""
        predictions = [
            [False, True, True, True]  # correct, error, error, error
        ]
        expected = [
            [False]  # only the correct word remains
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_single_word_error(self):
        """Test single phrase with single error word (should not be trimmed)."""
        predictions = [
            [True]  # single error word
        ]
        expected = [
            [True]  # unchanged due to single word rule
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_single_phrase_single_word_correct(self):
        """Test single phrase with single correct word."""
        predictions = [
            [False]  # single correct word
        ]
        expected = [
            [False]  # unchanged
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_empty_predictions_list(self):
        """Test behavior with empty predictions list."""
        predictions = []
        # Should handle gracefully and return original
        result = trim_predictions(predictions=predictions)
        assert result == predictions

    def test_phrase_becomes_empty_after_trimming(self):
        """Test phrases that become empty after trimming are removed."""
        predictions = [
            [False, True],  # correct, error
            [True, True],  # all errors (should be removed)
        ]
        # The function trims to the last non-error, which is the False in first phrase
        expected = [
            [False]  # trimmed to last non-error
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_complex_multi_phrase_scenario(self):
        """Test complex scenario with multiple phrases and various patterns."""
        predictions = [
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

    def test_only_last_phrase_has_trailing_errors(self):
        """Test when only the last phrase needs trimming."""
        predictions = [
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
    def test_exception_handling_returns_original(self, mock_logger):
        """Test that exceptions are caught and original predictions returned."""
        # Test the actual exception case we saw - all errors scenario
        predictions = [
            [True, True],  # all errors
            [True, True, True],  # all errors
        ]

        # This should trigger the exception handling path and return original
        result = trim_predictions(predictions=predictions)

        # Should return original predictions when exception occurs
        assert result == predictions

        # Verify that a warning was logged
        mock_logger.warning.assert_called_once()

    def test_edge_case_last_phrase_only_errors(self):
        """Test when last phrase contains only errors."""
        predictions = [
            [False, True, False],  # phrase 1: mixed
            [True, True],  # phrase 2: only errors
        ]
        expected = [
            [False, True, False]  # phrase 1: unchanged, phrase 2 removed
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_alternating_pattern_with_trailing_errors(self):
        """Test alternating correct/error pattern with trailing errors."""
        predictions = [
            [False, True, False, True, False],  # alternating, ends correct
            [True, False, True, True, True],  # alternating then trailing errors
        ]
        expected = [
            [False, True, False, True, False],  # unchanged
            [True, False],  # trimmed to last correct
        ]
        result = trim_predictions(predictions=predictions)
        assert result == expected

    def test_nested_empty_phrases_handling(self):
        """Test handling of edge cases with phrase structure."""
        predictions = [
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
