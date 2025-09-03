import pytest

try:
    from my_asr_aligner import word_level_alignment
except Exception:  # pragma: no cover - extension may be missing locally
    word_level_alignment = None  # type: ignore[assignment]


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_basic():
    expected = ["cat", "sat"]
    ref = ["k", "æ", "t", "s", "æ", "t"]
    hyp = ["k", "æ", "t", "s", "æ", "t"]
    conf = [0.9, 0.8, 0.95, 0.7, 0.85, 0.9]

    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert len(words) == len(hyp)
    assert not any(errors)
    assert all(c >= 0.0 for c in matched_conf)


@pytest.mark.skipif(word_level_alignment is None, reason="aligner extension not built")
def test_word_level_alignment_with_errors():
    expected = ["cat", "sat"]
    ref = ["k", "æ", "t", "s", "æ", "t"]
    hyp = ["k", "æ", "p", "s", "æ", "t"]  # one wrong phoneme
    conf = [0.9, 0.8, 0.95, 0.7, 0.85, 0.9]

    words, errors, matched_conf = word_level_alignment(expected, ref, hyp, conf)
    assert len(words) == len(hyp)
    assert any(errors)
    assert all(c >= 0.0 for c in matched_conf)
