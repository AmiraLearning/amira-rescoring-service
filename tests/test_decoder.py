from typing import Final

import numpy as np
import pytest

from src.pipeline.inference.decoder import PhonemeDecoder

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
