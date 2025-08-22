from typing import List, Tuple

__all__ = ["word_level_alignment"]

def word_level_alignment(
    *,
    expected_items: List[str],
    ref_phons: List[str],
    hyp_phons: List[str],
    confidences: List[float],
) -> Tuple[List[str], List[bool], List[float]]: ...
