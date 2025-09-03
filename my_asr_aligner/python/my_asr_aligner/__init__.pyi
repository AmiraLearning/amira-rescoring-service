__all__ = ["word_level_alignment"]

def word_level_alignment(
    *,
    expected_items: list[str],
    ref_phons: list[str],
    hyp_phons: list[str],
    confidences: list[float],
) -> tuple[list[str], list[bool], list[float]]: ...
