import difflib
import re
from enum import Enum
from itertools import product
from typing import Any, Final, cast

import jellyfish
import metaphone
from Bio import pairwise2

import amira_fe.phonetic_algorithms_es as metaphoneES
from amira_fe.es_soundex import spanish_soundex
from amira_fe.prod_word_slicing import align_words
from amira_pyutils.abstract_alignment import (
    UNK_TOKEN,
    AlignmentConfig,
    AlignmentResult,
)
from amira_pyutils.functional import fmap_opt, or_else
from amira_pyutils.language import LanguageHandling
from amira_pyutils.logging import get_logger
from amira_pyutils.text import normalize

_PA: Final = metaphoneES.PhoneticAlgorithmsES()
_LOGGER: Final = get_logger(__name__)

_SUPPORTED_LANGUAGES: Final = LanguageHandling


class CoarseSimilarity(Enum):
    """Enumeration for coarse similarity levels between strings."""

    LOW = 0
    MED = 1
    HI = 2


def _invalid_language(lang: LanguageHandling) -> None:
    """Raises ValueError for unsupported language.

    Args:
        lang: The unsupported language handling enum value.

    Raises:
        ValueError: Always raised with descriptive error message.
    """
    err_desc = f"Unsupported language specified: {lang}"
    _LOGGER.error(err_desc)
    raise ValueError(err_desc)


def _phon_compare(str1: str, str2: str, lang: LanguageHandling) -> bool:
    """Compare two strings for phonetic similarity using language-specific algorithms.

    Uses metaphone, metaphone spanish, and soundex spanish algorithms depending
    on the specified language to determine if words are phonetically similar.

    Args:
        str1: First word to compare.
        str2: Second word to compare.
        lang: Language of origin for the words, used to select the appropriate
            phonetic encoding algorithm. Must be from _SUPPORTED_LANGUAGES.

    Returns:
        True if words are phonetically similar, False otherwise.

    Raises:
        ValueError: If unsupported language is specified.
    """

    def _metaphone_similarity(mp1: str, mp2: str) -> bool:
        """Check if one metaphone code is contained in another."""
        return (mp1 in mp2) or (mp2 in mp1)

    match lang:
        case LanguageHandling.ENGLISH:
            result = jellyfish.match_rating_comparison(str1, str2)
            if result is None:
                if len(str1) < len(str2):
                    str2 = str2[: len(str1) + 2]
                else:
                    str1 = str1[: len(str2) + 2]
                result = jellyfish.match_rating_comparison(str1, str2)

            return bool(or_else(False, result))
        case LanguageHandling.ENGLISH_METAPHONE:

            def _norm_double_metaphone(mp: tuple[str, str]) -> tuple[str, str]:
                """Normalize double metaphone result by duplicating primary if secondary is empty."""
                return mp if mp[1] != "" else (mp[0], mp[0])

            def _double_metaphone_similarity(dmp1: tuple[str, str], dmp2: tuple[str, str]) -> bool:
                """Check similarity between double metaphone results."""
                return any([_metaphone_similarity(mp1, mp2) for mp1, mp2 in product(dmp1, dmp2)])

            meta1 = _norm_double_metaphone(metaphone.doublemetaphone(str1))
            meta2 = _norm_double_metaphone(metaphone.doublemetaphone(str2))
            return _double_metaphone_similarity(meta1, meta2)
        case LanguageHandling.SPANISH_METAPHONE:
            return _metaphone_similarity(_PA.metaphone(str1), _PA.metaphone(str2))
        case LanguageHandling.SPANISH_SOUNDEX:

            def _normed_spanish_soundex(s: str) -> str:
                """Get normalized Spanish soundex, falling back to original string if None."""
                return str(or_else(s, fmap_opt(spanish_soundex, s)))

            return _metaphone_similarity(
                _normed_spanish_soundex(str1), _normed_spanish_soundex(str2)
            )
        case _:
            _invalid_language(lang)
            return False  # This line is never reached due to exception above


HI_THRESH_LEV: Final[float] = 0.65
LO_THRESH_LEV: Final[float] = 0.5


def _lev_compare(str1: str, str2: str) -> CoarseSimilarity:
    """Compare strings using Levenshtein distance to determine similarity level.

    Calculates the ratio of similarity based on string lengths and edit distance,
    then categorizes into high, medium, or low similarity levels.

    Args:
        str1: First word to compare.
        str2: Second word to compare.

    Returns:
        CoarseSimilarity.HI if words are close in edit distance,
        CoarseSimilarity.MED if words are questionably close,
        CoarseSimilarity.LOW if words are far apart in edit distance.
    """
    len1 = len(str1)
    len2 = len(str2)
    lev = jellyfish.levenshtein_distance(str1, str2)
    ratio = (len1 + len2 - lev) / (len1 + len2)
    if ratio >= HI_THRESH_LEV:
        return CoarseSimilarity.HI
    elif (ratio < HI_THRESH_LEV) and (ratio >= LO_THRESH_LEV):
        return CoarseSimilarity.MED
    else:
        return CoarseSimilarity.LOW


HI_THRESH_LETT: Final[float] = 1
LO_THRESH_LETT: Final[float] = 0.5


def _lett_compare(str1: str, str2: str) -> CoarseSimilarity:
    """Compare strings based on overlapping and non-overlapping letters.

    Determines how many letters the strings have in common versus how many
    are different, using this ratio to judge similarity level.

    Args:
        str1: First word to compare.
        str2: Second word to compare.

    Returns:
        CoarseSimilarity.HI if words have many letters in common,
        CoarseSimilarity.MED if words have some letters in common,
        CoarseSimilarity.LOW if words have few or no letters in common.
    """

    let_diff = len(set(str1).symmetric_difference(str2))
    let_overlap = len(set(str1).intersection(str2))
    if let_diff == 0:
        ratio = float(let_overlap)
    else:
        ratio = let_overlap / let_diff

    if ratio >= HI_THRESH_LETT:
        return CoarseSimilarity.HI
    elif (ratio < HI_THRESH_LETT) and (ratio >= LO_THRESH_LETT):
        return CoarseSimilarity.MED
    else:
        return CoarseSimilarity.LOW


def _first_lett_compare(str1: str, str2: str) -> bool:
    """Check if two strings begin with the same letter.

    Args:
        str1: First word to compare.
        str2: Second word to compare.

    Returns:
        True if words begin with the same letter, False otherwise.
    """
    return str1[0] == str2[0]


LETTER_NAME_SOUND_RE: Final[re.Pattern[str]] = re.compile(r"[a-z](?:_?(sound|letter))?")


def _check_letter_name_sound(s: str) -> bool:
    """Check if string matches letter name/sound patterns.

    Checks if a string is of one of the forms:
    - {letter}
    - {letter}sound
    - {letter}_sound
    - {letter}letter
    - {letter}_letter

    Args:
        s: String to check.

    Returns:
        True if string matches letter name/sound pattern, False otherwise.
    """
    return bool(re.fullmatch(LETTER_NAME_SOUND_RE, s.lower()))


def _compare_strings(str1: str, str2: str, lang: LanguageHandling) -> bool:
    """Compare two strings for phonetic and edit distance similarity.

    Determines if strings are phonetically similar and within reasonable
    edit distance of each other using multiple comparison methods.

    Args:
        str1: First word to compare.
        str2: Second word to compare.
        lang: Language of origin for phonetic encoding. Must be from
            _SUPPORTED_LANGUAGES.

    Returns:
        True if words are similar, False otherwise.
    """
    both_letters = _check_letter_name_sound(str1) and _check_letter_name_sound(str2)
    first_lett = _first_lett_compare(str1, str2)

    if both_letters:
        return first_lett

    lev = _lev_compare(str1, str2)
    phon = _phon_compare(str1, str2, lang=lang)

    if lev == CoarseSimilarity.HI:
        return phon or first_lett

    lett = _lett_compare(str1, str2)

    match lev:
        case CoarseSimilarity.MED:
            match lett:
                case CoarseSimilarity.HI:
                    if phon or first_lett:
                        return True
                    else:
                        return False
                case CoarseSimilarity.LOW:
                    if phon and first_lett:
                        return True
                    elif (lang == LanguageHandling.SPANISH_METAPHONE) or (
                        lang == LanguageHandling.SPANISH_SOUNDEX
                    ):
                        return (str1 == "y" or str1 == "e") and (str2 == "y" or str2 == "e")
                    else:
                        return False
                case _:
                    return first_lett
        case _:
            return False


def align_patch(
    expected: list[str],
    actual: list[str],
    next_exp: str = "",
    expected_index: list[Any] | None = None,
    actual_index: list[Any] | None = None,
    lang: LanguageHandling = LanguageHandling.ENGLISH,
) -> tuple[list[str], list[str], list[Any] | None, list[Any] | None]:
    """Fix alignment for pre-aligned dash-separated strings to prioritize repair over reparandum.

    This function is NOT optimized and could be improved with linear algebra
    operations instead of loops if necessary.

    Example:
        Expected: 'I am'
        Transcript: 'I I am'
        Original expected aligned: ['I', '-', 'am']
        Original transcript aligned: ['I', 'I', 'am']
        Patched expected aligned: ['-', 'I', 'am']
        Patched transcript aligned: ['I', 'I', 'am']

    Args:
        expected: Pre-aligned, dash-separated words from expected text (story text).
            Should have same length as actual.
        actual: Pre-aligned, dash-separated words from transcript (raw ASR output).
            Should have same length as expected.
        next_exp: Optional next expected word in the story. Prevents patch from
            aligning previous word to next word.
        expected_index: Optional metadata list same length as expected containing
            metadata for each corresponding word. Should also be '-' separated.
        actual_index: Optional metadata list same length as actual. If both
            index lists are provided, metadata will be sorted alongside words.
        lang: Language/algorithm for phonetic comparison. Must be from
            _SUPPORTED_LANGUAGES.

    Returns:
        Tuple containing:
        - Align-corrected, dash-separated list of expected text
        - Align-corrected, dash-separated list of actual text
        - Align-corrected expected metadata (if provided)
        - Align-corrected actual metadata (if provided)

    Raises:
        ValueError: If unsupported language specified.
        Exception: If pre-aligned texts are not same length or metadata
            lists are incorrect length.
    """
    repair: list[int] = []
    force_misalign = False

    if lang not in _SUPPORTED_LANGUAGES:
        _invalid_language(lang)

    if len(expected) != len(actual):
        _LOGGER.info("Incorrect pre-alignment")
        raise Exception("Pre-aligned texts are not the same length")

    meta = False
    if (actual_index is not None) and (expected_index is not None):
        meta = True
        if (len(expected_index) != len(actual_index)) or (len(expected_index) != len(expected)):
            _LOGGER.info("Incorrect metadata length")
            raise Exception("Metadata lists are the incorrect length")

    i = len(expected) - 1
    while i >= 0:
        if (expected[i] != "-") and (actual[i] != "-"):
            if (not _compare_strings(expected[i], actual[i], lang=lang)) or (
                actual[i] == UNK_TOKEN
            ):
                expected.insert(i, "-")
                if meta:
                    assert expected_index is not None
                    expected_index.insert(i, "-")
                actual.insert(i + 1, "-")
                if meta:
                    assert actual_index is not None
                    actual_index.insert(i + 1, "-")

                i += 1
                repair = [x + 1 for x in repair]
                force_misalign = True

        if expected[i] == "-":
            repair.append(i)
        elif (expected[i] != "-") and (len(repair) != 0):
            for j in range(len(repair) - 1, -1, -1):
                check1 = _compare_strings(expected[i], actual[repair[j]], lang=lang)

                if (actual[i] == "-") and (not force_misalign):
                    check2 = True
                elif (actual[i] == "-") and force_misalign:
                    check2 = jellyfish.levenshtein_distance(expected[i], actual[repair[j]]) <= (
                        jellyfish.levenshtein_distance(expected[i], actual[i - 1]) + 1
                    )
                else:
                    if jellyfish.levenshtein_distance(expected[i], actual[i]) == 0:
                        check2 = jellyfish.levenshtein_distance(expected[i], actual[repair[j]]) == 0
                    else:
                        check2 = jellyfish.levenshtein_distance(expected[i], actual[repair[j]]) <= (
                            jellyfish.levenshtein_distance(expected[i], actual[i]) + 1
                        )

                check3 = actual[repair[j]] != next_exp
                check4 = actual[repair[j]] != UNK_TOKEN

                if check1 and check2 and check3 and check4:
                    force_misalign = False

                    expected[repair[j]] = expected[i]
                    expected[i] = "-"
                    if meta:
                        assert expected_index is not None
                        expected_index[repair[j]] = expected_index[i]
                        expected_index[i] = "-"

                    if actual[i] == "-":
                        actual.pop(i)
                        if meta:
                            assert actual_index is not None
                            actual_index.pop(i)
                        expected.pop(i)
                        if meta:
                            assert expected_index is not None
                            expected_index.pop(i)
                        repair = [x - 1 for x in repair]

                    i = repair[j]

            if force_misalign or (actual[i] == "-"):
                if repair[0] == len(actual) - 1:
                    actual.append(actual[i])
                    if meta:
                        assert actual_index is not None
                        actual_index.append(actual_index[i])
                    expected.append(expected[i])
                    if meta:
                        assert expected_index is not None
                        expected_index.append(expected_index[i])
                else:
                    actual.insert(repair[0] + 1, actual[i])
                    if meta:
                        assert actual_index is not None
                        actual_index.insert(repair[0] + 1, actual_index[i])
                    expected.insert(repair[0] + 1, expected[i])
                    if meta:
                        assert expected_index is not None
                        expected_index.insert(repair[0] + 1, expected_index[i])

                actual.pop(i)
                if meta:
                    assert actual_index is not None
                    actual_index.pop(i)
                expected.pop(i)
                if meta:
                    assert expected_index is not None
                    expected_index.pop(i)

                i = repair[0]

            repair = []
        force_misalign = False

        if expected[i] != "-":
            next_exp = expected[i]

        i -= 1

    if meta:
        return expected, actual, expected_index, actual_index
    else:
        return expected, actual, None, None


def align_dash(
    first: str, second: str, lang: LanguageHandling, use_simple_diff: bool
) -> tuple[list[str], list[str], list[int | str], list[int | str]]:
    """Normalize strings and align second against first using diff or pairwise alignment.

    Modified from align() from amira_pyutils.data.text.py. Normalizes strings
    and aligns second against first (e.g., ASR output against expected text).

    Args:
        first: Reference text to align against.
        second: Text to be aligned to the reference.
        lang: Language for normalization.
        use_simple_diff: If True, use simple diff; otherwise use pairwise alignment.

    Returns:
        Tuple containing:
        - Aligned first text as list of words
        - Aligned second text as list of words
        - Word indices for first text
        - Word indices for second text
    """

    def create_index(str_list: list[str]) -> list[int | str]:
        """Create index mapping for word positions, using '-' for gaps."""
        meta_index: list[int | str] = []
        idx = 0
        for w in str_list:
            if w != "-":
                meta_index.append(idx)
                idx += 1
            else:
                meta_index.append("-")
        return meta_index

    first_words = normalize(text=first, lang=lang)
    second_words = normalize(text=second, lang=lang)

    if use_simple_diff:
        # Simple alignment using SequenceMatcher
        matcher = difflib.SequenceMatcher(None, first_words, second_words)
        aligned_first: list[str] = []
        aligned_second: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal" or tag == "replace":
                aligned_first.extend(first_words[i1:i2])
                aligned_second.extend(second_words[j1:j2])
                # Pad shorter sequence
                while len(aligned_first) > len(aligned_second):
                    aligned_second.append("-")
                while len(aligned_second) > len(aligned_first):
                    aligned_first.append("-")
            elif tag == "delete":
                aligned_first.extend(first_words[i1:i2])
                aligned_second.extend(["-"] * (i2 - i1))
            elif tag == "insert":
                aligned_first.extend(["-"] * (j2 - j1))
                aligned_second.extend(second_words[j1:j2])
        first_words = aligned_first
        second_words = aligned_second
    else:
        if len(second_words) == 0:
            second_words = ["-" for _ in first_words]
        else:
            MATCH_REWARD = 1
            MISMATCH_PENALTY = -1
            GAP_PENALTY = 0
            GAP_EXTENSION_PENALTY = 0
            alignments = pairwise2.align.globalms(
                first_words,
                second_words,
                MATCH_REWARD,
                MISMATCH_PENALTY,
                GAP_PENALTY,
                GAP_EXTENSION_PENALTY,
                gap_char=["-"],
                one_alignment_only=True,
            )
            first_words = list(alignments[0].seqA)
            second_words = list(alignments[0].seqB)

    first_word_index = create_index(first_words)
    second_word_index = create_index(second_words)

    return first_words, second_words, first_word_index, second_word_index


def align_texts_dash(
    config: AlignmentConfig,
    lang: LanguageHandling,
    next_exp: str | None,
    text1: str,
    text2: str,
) -> AlignmentResult:
    """Align and repair alignment of two strings with comprehensive post-processing.

    Modified from align_texts() from speech.py. Wrapper function to align and
    repair alignment of two strings with various preprocessing and postprocessing
    options controlled by the configuration.

    Args:
        config: Configuration object controlling alignment behavior.
        lang: Language/algorithm for phonetic comparison. Must be from
            _SUPPORTED_LANGUAGES.
        next_exp: Optional next expected word in the story. Prevents patch
            from aligning previous word to next word.
        text1: Reference text to align against.
        text2: Text to be matched to the reference text.

    Returns:
        Tuple containing:
        - Aligned version of text1
        - Aligned version of text2
        - Aligned word indexes of text1
        - Aligned word indexes of text2
        - Raw aligned version of text1 (no post-processing)
        - Raw aligned version of text2 (no post-processing)
        - Prefixes for each aligned position
    """
    if next_exp is None:
        next_exp = ""
    else:
        next_exp = next_exp.lower()

    _LOGGER.debug(
        message=f'align_texts_dash: Reference_text="{text1}", Text_to_be_matched="{text2}"'
    )

    text1 = text1.split("NOTE", 1)[0]
    text2 = text2.split("NOTE", 1)[0]

    dropped_suffix_from: int | None = None
    t2_words: list[str] | None = None
    if config.preprocess_next_expected and (next_exp is not None):
        t1_words = normalize(text=text1, lang=lang)
        t2_words = normalize(text=text2, lang=lang)
        if len(t2_words) > len(t1_words):
            last_pos_expected = max(
                [idx if t1_words[idx] == next_exp else -3 for idx in range(-2, 0)]
            )
            for idx in range(-1, last_pos_expected, -1):
                if t2_words[idx] == next_exp:
                    dropped_suffix_from = idx
                    break
        if dropped_suffix_from is not None:
            if t2_words is not None:
                text2 = " ".join(t2_words[:dropped_suffix_from])

    # Note: use_prod is not a standard AlignmentConfig attribute, using simple_initial_diff as proxy
    if not config.simple_initial_diff:
        expected = " ".join(normalize(text=text1, lang=lang))
        transcribed = " ".join(normalize(text=text2, lang=lang))
        aligned_text1, aligned_text2, text1_index, text2_index = align_words(
            base_txt=expected, var_txt=transcribed
        )
    else:
        aligned_text1, aligned_text2, text1_index, text2_index = align_dash(
            text1, text2, lang, config.simple_initial_diff
        )
        (
            aligned_text1,
            aligned_text2,
            patched_text1_index,
            patched_text2_index,
        ) = align_patch(
            aligned_text1,
            aligned_text2,
            next_exp=next_exp,
            expected_index=text1_index,
            actual_index=text2_index,
            lang=lang,
        )

        if patched_text1_index is None or patched_text2_index is None:
            raise ValueError("align_patch returned None indices despite metadata input")

        text1_index = cast(list[int | str], patched_text1_index)
        text2_index = cast(list[int | str], patched_text2_index)

    raw_text1 = aligned_text1
    raw_text2 = aligned_text2

    # The indexes are already the correct type from align_patch

    if config.postprocess_inference:
        dumped_t1_index: list[int] = []
        dumped_t2_index: list[int] = []
        for idx in range(len(aligned_text1)):
            if aligned_text1[idx] != "-":
                if aligned_text2[idx] != "-":
                    for idx2 in range(min(len(dumped_t1_index), len(dumped_t2_index))):
                        aligned_text2[dumped_t1_index[idx2]] = aligned_text2[dumped_t2_index[idx2]]
                        text2_index[dumped_t1_index[idx2]] = text2_index[dumped_t2_index[idx2]]
                        aligned_text2[dumped_t2_index[idx2]] = "-"
                        text2_index[dumped_t2_index[idx2]] = "-"
                    dumped_t1_index = []
                    dumped_t2_index = []
                else:
                    dumped_t1_index.append(idx)
            elif aligned_text2[idx] != "-":
                dumped_t2_index.append(idx)

        for idx2 in range(min(len(dumped_t1_index), len(dumped_t2_index))):
            aligned_text2[dumped_t1_index[idx2]] = aligned_text2[dumped_t2_index[idx2]]
            text2_index[dumped_t1_index[idx2]] = text2_index[dumped_t2_index[idx2]]
            aligned_text2[dumped_t2_index[idx2]] = "-"
            text2_index[dumped_t2_index[idx2]] = "-"

    if dropped_suffix_from is not None:
        pad = -dropped_suffix_from
        aligned_text1.extend(["-"] * pad)
        text1_index.extend(["-"] * pad)
        if t2_words is not None:
            aligned_text2.extend(t2_words[dropped_suffix_from:])
        int_indices = [v for v in text2_index if isinstance(v, int)]
        next_t2_value: int = max(int_indices, default=-1)
        start: int = next_t2_value + 1
        stop: int = next_t2_value + pad + 1
        text2_index.extend(list(range(start, stop)))

    _LOGGER.debug(message=f'Aligned strings with meta indexing, text1="{text1}", text2="{text2}"')

    idx = 0
    prefix = []
    prefixes = []
    while idx < len(aligned_text1):
        if text1_index[idx] == "-":
            prefix.append(aligned_text2[idx])
            prefixes.append("")
        else:
            prefixes.append(" ".join(prefix))
            prefix = []
        idx += 1

    return AlignmentResult(
        aligned_reference=aligned_text1,
        aligned_transcription_features=cast(list[Any], aligned_text2),
        reference_word_indices=[i if isinstance(i, int) else -1 for i in text1_index],
        transcription_word_indices=[i if isinstance(i, int) else -1 for i in text2_index],
        raw_aligned_reference=raw_text1,
        raw_aligned_transcription_features=cast(list[Any], raw_text2),
    )
