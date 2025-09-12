from collections.abc import Callable
from typing import Any

import pytest

try:
    from my_asr_aligner import word_level_alignment  # type: ignore
except Exception:  # pragma: no cover - extension may be missing locally

    def _stub_alignment(
        expected: list[str],
        ref: list[str],
        hyp: list[str],
        conf: list[float],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[str], list[bool], list[float]]:
        return hyp, [False] * len(hyp), conf[: len(hyp)]

    word_level_alignment: Callable[..., tuple[list[str], list[bool], list[float]]] | None = (
        _stub_alignment
    )


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_basic() -> None:
    expected = ["cat", "sat"]
    ref = ["k", "æ", "t", "s", "æ", "t"]
    hyp = ["k", "æ", "t", "s", "æ", "t"]
    conf = [0.9, 0.8, 0.95, 0.7, 0.85, 0.9]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert len(words) == len(hyp)
    assert not any(errors)
    assert all(c >= 0.0 for c in matched_conf)


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_with_errors() -> None:
    expected = ["cat", "sat"]
    ref = ["k", "æ", "t", "s", "æ", "t"]
    hyp = ["k", "æ", "p", "s", "æ", "t"]  # one wrong phoneme
    conf = [0.9, 0.8, 0.95, 0.7, 0.85, 0.9]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert len(words) == len(hyp)
    assert any(errors)
    assert all(c >= 0.0 for c in matched_conf)


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_pure_insertion_exposed() -> None:
    expected = ["a_letter"]
    ref = ["a"]
    hyp = ["a", "b"]
    conf = [0.9, 0.33]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert words == hyp
    assert len(errors) == 2
    assert matched_conf[1] == pytest.approx(0.33, rel=1e-6)


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_confidence_weighting_halves_error_confidence() -> None:
    expected = ["a_letter"]
    ref = ["a"]
    hyp = ["x"]  # invalid phoneme for a_letter -> error
    conf = [0.8]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    words, errors, matched_conf = word_level_alignment(
        expected, ref, hyp, conf, enable_confidence_weighting=True
    )
    assert errors == [True]
    assert matched_conf[0] == pytest.approx(0.4, rel=1e-6)
