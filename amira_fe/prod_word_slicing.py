import json
from typing import Final

from amira_fe.blank_detect_pb2 import BlankDetectMessage
from amira_fe.blank_detect_utils import get_blank_detector

_DASH_PLACEHOLDER: Final[str] = "-"


def align_words(
    *, base_txt: str, var_txt: str
) -> tuple[list[str], list[str], list[int | str], list[int | str]]:
    """Align words between base text and variant text using blank detection.

    This function performs sequence alignment between two texts by:
    1. Using a blank detector to identify differences between expected and actual text
    2. Processing the diff to create aligned sequences where:
       - Removed words appear in base but not variant (marked with "-" in variant)
       - Added words appear in variant but not base (marked with "-" in base)
       - Matched words appear in both sequences at corresponding positions
    3. Handling delta adjustments for proper alignment when words are added after removals

    The alignment process maintains positional indices for both sequences to track
    original word positions in the unaligned texts.

    Args:
        base_txt: Reference text to align against
        var_txt: Variant text to align with reference

    Returns:
        Tuple containing:
        - ref_aligned: Aligned reference words (with "-" for insertions)
        - var_aligned: Aligned variant words (with "-" for deletions)
        - ref_aligned_index: Original indices for reference words ("-" for insertions)
        - var_aligned_index: Original indices for variant words ("-" for deletions)
    """
    txt_align = get_blank_detector()
    msg = BlankDetectMessage(expected=base_txt, actual=var_txt)
    out = txt_align.blankdetect(msg)
    diff_data: list[dict] = json.loads(out.alignment)["diff"]

    ref_words: list[str] = base_txt.split()
    var_words: list[str] = var_txt.split()

    ref_idx: int = 0
    var_idx: int = 0
    ref_aligned: list[str] = []
    var_aligned: list[str] = []
    ref_aligned_index: list[int | str] = []
    var_aligned_index: list[int | str] = []
    delta: int = 0

    for diff_entry in diff_data:
        count: int = diff_entry["count"]

        for _ in range(count):
            if diff_entry.get("removed"):
                _handle_removed_word(
                    ref_words=ref_words,
                    ref_idx=ref_idx,
                    ref_aligned=ref_aligned,
                    var_aligned=var_aligned,
                    ref_aligned_index=ref_aligned_index,
                    var_aligned_index=var_aligned_index,
                )
                ref_idx += 1
                delta += 1
            elif diff_entry.get("added"):
                delta = _handle_added_word(
                    var_words=var_words,
                    var_idx=var_idx,
                    delta=delta,
                    ref_aligned=ref_aligned,
                    var_aligned=var_aligned,
                    ref_aligned_index=ref_aligned_index,
                    var_aligned_index=var_aligned_index,
                )
                var_idx += 1
            else:
                delta = 0
                _handle_matched_word(
                    ref_words=ref_words,
                    var_words=var_words,
                    ref_idx=ref_idx,
                    var_idx=var_idx,
                    ref_aligned=ref_aligned,
                    var_aligned=var_aligned,
                    ref_aligned_index=ref_aligned_index,
                    var_aligned_index=var_aligned_index,
                )
                ref_idx += 1
                var_idx += 1

    return ref_aligned, var_aligned, ref_aligned_index, var_aligned_index


def _handle_removed_word(
    *,
    ref_words: list[str],
    ref_idx: int,
    ref_aligned: list[str],
    var_aligned: list[str],
    ref_aligned_index: list[int | str],
    var_aligned_index: list[int | str],
) -> None:
    """Handle a word that was removed from the variant text.

    Args:
        ref_words: List of reference words
        ref_idx: Index of reference word
        ref_aligned: List of aligned reference words
        var_aligned: List of aligned variant words
        ref_aligned_index: List of aligned reference indices
        var_aligned_index: List of aligned variant indices
    """
    ref_aligned.append(ref_words[ref_idx])
    ref_aligned_index.append(ref_idx)
    var_aligned.append(_DASH_PLACEHOLDER)
    var_aligned_index.append(_DASH_PLACEHOLDER)


def _handle_added_word(
    *,
    var_words: list[str],
    var_idx: int,
    delta: int,
    ref_aligned: list[str],
    var_aligned: list[str],
    ref_aligned_index: list[int | str],
    var_aligned_index: list[int | str],
) -> int:
    """Handle a word that was added in the variant text.

    Returns:
        Updated delta value after processing the addition.
    """
    if delta > 0:
        var_aligned[-delta] = var_words[var_idx]
        var_aligned_index[-delta] = var_idx
        return delta - 1
    else:
        var_aligned.append(var_words[var_idx])
        var_aligned_index.append(var_idx)
        ref_aligned.append(_DASH_PLACEHOLDER)
        ref_aligned_index.append(_DASH_PLACEHOLDER)
        return delta


def _handle_matched_word(
    *,
    ref_words: list[str],
    var_words: list[str],
    ref_idx: int,
    var_idx: int,
    ref_aligned: list[str],
    var_aligned: list[str],
    ref_aligned_index: list[int | str],
    var_aligned_index: list[int | str],
) -> None:
    """Handle a word that matches between reference and variant texts.

    Args:
        ref_words: List of reference words
        var_words: List of variant words
        ref_idx: Index of reference word
        var_idx: Index of variant word
        ref_aligned: List of aligned reference words
        var_aligned: List of aligned variant words
        ref_aligned_index: List of aligned reference indices
        var_aligned_index: List of aligned variant indices
    """
    ref_aligned.append(ref_words[ref_idx])
    ref_aligned_index.append(ref_idx)
    var_aligned.append(var_words[var_idx])
    var_aligned_index.append(var_idx)
