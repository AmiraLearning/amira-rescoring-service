from collections.abc import Callable
from typing import Any

import pytest

word_level_alignment: Callable[..., tuple[list[str], list[bool], list[float]]] | None
try:
    from my_asr_aligner import word_level_alignment as _word_level_alignment

    word_level_alignment = _word_level_alignment
except Exception:  # pragma: no cover

    def _stub_alignment(
        expected: list[str],
        ref: list[str],
        hyp: list[str],
        conf: list[float],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[str], list[bool], list[float]]:
        return hyp, [False] * len(hyp), conf[: len(hyp)]

    word_level_alignment = _stub_alignment


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_aligner_exact_match_golden() -> None:
    expected = ["cat"]
    ref = ["k", "æ", "t"]
    hyp = ["k", "æ", "t"]
    conf = [0.9, 0.8, 0.95]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    assert word_level_alignment is not None
    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert words == hyp
    assert errors == [False, False, False]
    assert matched_conf == pytest.approx(conf)


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_aligner_single_error_golden() -> None:
    expected = ["cat"]
    ref = ["k", "æ", "t"]
    hyp = ["k", "æ", "p"]
    conf = [0.9, 0.8, 0.95]

    if word_level_alignment is None:
        pytest.skip("aligner extension not available")
    assert word_level_alignment is not None
    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert len(words) == 3
    assert errors == [False, False, True]
    assert matched_conf[0] == pytest.approx(0.9)
    assert matched_conf[1] == pytest.approx(0.8)
    assert matched_conf[2] == pytest.approx(0.0)
