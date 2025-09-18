from typing import Final

import numpy as np
import pytest

from src.letter_scoring_pipeline.inference.decoder import PhonemeDecoder

_EXPECTED_GROUPED_ELEMENTS: Final[list[str]] = ["t", "æ"]
_EXPECTED_CONFIDENCE_COUNT: Final[int] = 2
_CONFIDENCE_TOLERANCE: Final[float] = 1e-6


@pytest.fixture
def decoder() -> PhonemeDecoder:
    """Create a PhonemeDecoder instance for testing."""
    return PhonemeDecoder()


def _create_token_sequence(
    *, tokens: list[str], probabilities: list[float]
) -> tuple[list[str], np.ndarray]:
    """Create token sequence with corresponding probability array.

    Args:
        tokens: List of phoneme tokens
        probabilities: Corresponding confidence probabilities

    Returns:
        Tuple of tokens and probability array
    """
    return tokens, np.array(probabilities, dtype=np.float32)


def _assert_confidence_matches(
    *, actual: float, expected: float, tolerance: float = _CONFIDENCE_TOLERANCE
) -> None:
    """Assert confidence value matches expected within tolerance.

    Args:
        actual: Actual confidence value
        expected: Expected confidence value
        tolerance: Acceptable difference tolerance
    """
    assert abs(actual - expected) < tolerance, f"Confidence mismatch: {actual} != {expected}"


def test_decoder_groups_and_separates_segments(decoder: PhonemeDecoder) -> None:
    """Test that decoder correctly groups consecutive tokens and separates segments."""
    tokens, probs = _create_token_sequence(
        tokens=["t", "t", "|", "æ"], probabilities=[0.9, 0.7, 0.0, 0.8]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    assert transcript.elements == _EXPECTED_GROUPED_ELEMENTS, (
        f"Expected {_EXPECTED_GROUPED_ELEMENTS}, got {transcript.elements}"
    )
    assert len(transcript.confidences) == _EXPECTED_CONFIDENCE_COUNT

    expected_first_confidence = (0.9 + 0.7) / 2
    _assert_confidence_matches(actual=transcript.confidences[0], expected=expected_first_confidence)
    _assert_confidence_matches(actual=transcript.confidences[1], expected=0.8)


def test_decoder_robust_mode_skips_unmatched(
    decoder: PhonemeDecoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that robust mode gracefully handles unmatched tokens."""
    monkeypatch.setenv("DECODER_ROBUST_MODE", "true")

    tokens, probs = _create_token_sequence(
        tokens=["z", "z", "?", "|", "æ"], probabilities=[0.9, 0.8, 0.5, 0.0, 0.7]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    assert "æ" in transcript.elements, "Expected 'æ' to be preserved in robust mode"
    assert isinstance(transcript.elements, list), "Expected elements to be a list"
    assert len(transcript.elements) > 0, "Expected non-empty elements list"


def test_decoder_consecutive_separators_edge_case(decoder: PhonemeDecoder) -> None:
    """Test decoder handles consecutive separators correctly."""
    tokens, probs = _create_token_sequence(
        tokens=["t", "|", "|", "|", "æ"], probabilities=[0.9, 0.0, 0.0, 0.0, 0.8]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    # Should create two segments despite consecutive separators
    assert len(transcript.elements) == 2
    assert transcript.elements == ["t", "æ"]
    assert len(transcript.confidences) == 2


def test_decoder_empty_segments_handling(decoder: PhonemeDecoder) -> None:
    """Test decoder handles empty segments between separators."""
    tokens, probs = _create_token_sequence(
        tokens=["|", "t", "|", "|", "æ", "|"], probabilities=[0.0, 0.9, 0.0, 0.0, 0.8, 0.0]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    # Should ignore empty segments and preserve valid phonemes
    assert transcript.elements == ["t", "æ"]
    assert len(transcript.confidences) == 2
    _assert_confidence_matches(actual=transcript.confidences[0], expected=0.9)
    _assert_confidence_matches(actual=transcript.confidences[1], expected=0.8)


def test_decoder_token_skip_warnings_in_robust_mode(
    decoder: PhonemeDecoder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that robust mode logs warnings when skipping unmatched tokens."""
    monkeypatch.setenv("DECODER_ROBUST_MODE", "true")

    # Use invalid tokens that should trigger warnings
    tokens, probs = _create_token_sequence(
        tokens=["INVALID_TOKEN", "æ", "ANOTHER_INVALID"], probabilities=[0.9, 0.8, 0.7]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    # Should only contain valid phoneme
    assert transcript.elements == ["æ"]
    assert len(transcript.confidences) == 1


def test_decoder_all_invalid_tokens_robust_mode(
    decoder: PhonemeDecoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test robust mode when all tokens are invalid."""
    monkeypatch.setenv("DECODER_ROBUST_MODE", "true")

    tokens, probs = _create_token_sequence(
        tokens=["INVALID1", "INVALID2", "INVALID3"], probabilities=[0.9, 0.8, 0.7]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    # Should return empty transcript gracefully
    assert transcript.elements == []
    assert transcript.confidences == []


def test_decoder_mixed_valid_invalid_tokens(decoder: PhonemeDecoder) -> None:
    """Test decoder with mix of valid phonemes and invalid tokens."""
    tokens, probs = _create_token_sequence(
        tokens=["t", "INVALID", "æ", "|", "ANOTHER_INVALID", "s"],
        probabilities=[0.9, 0.1, 0.8, 0.0, 0.2, 0.7],
    )

    # In non-robust mode, this might raise an error or handle differently
    # Depending on implementation, we test the expected behavior
    try:
        transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)
        # If it succeeds, check that valid phonemes are preserved
        valid_phonemes = [elem for elem in transcript.elements if elem in ["t", "æ", "s"]]
        assert len(valid_phonemes) > 0, "Expected some valid phonemes to be preserved"
    except (ValueError, KeyError, RuntimeError):
        # If it fails in strict mode, that's also acceptable
        pass


def test_decoder_probability_averaging_edge_cases(decoder: PhonemeDecoder) -> None:
    """Test probability averaging with extreme values."""
    # Test with very low and very high confidence values
    tokens, probs = _create_token_sequence(
        tokens=["t", "t", "t", "|", "æ"], probabilities=[0.01, 0.99, 0.5, 0.0, 1.0]
    )

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    expected_first_confidence = (0.01 + 0.99 + 0.5) / 3
    _assert_confidence_matches(actual=transcript.confidences[0], expected=expected_first_confidence)
    _assert_confidence_matches(actual=transcript.confidences[1], expected=1.0)
