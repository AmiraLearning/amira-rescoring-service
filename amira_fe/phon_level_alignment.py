import re
from functools import reduce
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    pass

import edit_distance
import epitran
from fuzzywuzzy import fuzz
from toolz import curry

from amira_pyutils.abstract_alignment import (
    AlignmentConfig,
    AlignmentResult,
    WordSelectionStrategy,
)
from amira_pyutils.language import LanguageHandling
from amira_pyutils.logging import get_logger
from amira_pyutils.phone_alphabets import arpa2amirabet_ex
from amira_pyutils.text import normalize
from amira_pyutils.word_distance import l_dist

logger = get_logger(__name__)

_EPITRAN_SPANISH: Final[epitran.Epitran] = epitran.Epitran("spa-Latn")
_SUPPORTED_LANGUAGES: Final[MappingProxyType[LanguageHandling, list[str | None]]] = (
    MappingProxyType(
        {
            LanguageHandling.SPANISH_METAPHONE: [None, "ARPA"],
            LanguageHandling.SPANISH_SOUNDEX: [None, "ARPA"],
            LanguageHandling.ENGLISH: ["AMIRABET"],
        }
    )
)
_UNKNOWN_WORD_MARKER: Final[str] = "*"
_SKIP_MARKER: Final[str] = "-"
_SPACE_SEPARATOR: Final[str] = " "
_SILENCE_MARKER: Final[str] = "SIL"


def _validate_language_support(*, lang: LanguageHandling) -> None:
    """Validates that the specified language is supported.

    Args:
        lang: The language to validate.

    Raises:
        ValueError: If the language is not supported.
    """
    if lang not in _SUPPORTED_LANGUAGES:
        error_message = f"Unsupported language specified: {lang}"
        logger.error(error_message)
        raise ValueError(error_message)


def _intercalate_with_separator(*, items: list[str], separator: str) -> list[str]:
    """Intercalates a list with a separator, excluding the final separator.

    Args:
        items: List of items to intercalate.
        separator: Separator to insert between items.

    Returns:
        List with separators intercalated between items.
    """
    if not items:
        return []

    result = []
    for i, item in enumerate(items):
        result.append(item)
        if i < len(items) - 1:
            result.append(separator)

    return result


def _phonemize_to_ipa(*, lang: LanguageHandling, text: str) -> str:
    """Converts text to IPA phonetic representation.

    Args:
        lang: Language handling configuration.
        text: Text to phonemize.

    Returns:
        IPA phonetic representation of the text.
    """
    normalized_words = normalize(text=text, lang=lang)
    result = _EPITRAN_SPANISH.transliterate(_SPACE_SEPARATOR.join(normalized_words))
    return str(result)


def _phonemize_to_arpa(
    *, lang: LanguageHandling, text: str, arpa_dict: dict[str, list[str]]
) -> str:
    """Converts text to ARPA phonetic representation using dictionary lookup.

    Args:
        lang: Language handling configuration.
        text: Text to phonemize.
        arpa_dict: Dictionary mapping words to ARPA phonemes.

    Returns:
        ARPA phonetic representation with silence markers.
    """
    normalized_words = normalize(text=text, lang=lang)
    arpa_phonemes = [" ".join(arpa_dict.get(word.upper(), [""])) for word in normalized_words]
    intercalated = _intercalate_with_separator(items=arpa_phonemes, separator=_SILENCE_MARKER)
    return _SPACE_SEPARATOR.join(intercalated)


def _phonemize_to_amirabet(
    *, lang: LanguageHandling, text: str, arpa_dict: dict[str, list[str]]
) -> str:
    """Converts text to AMIRABET phonetic representation.

    Args:
        lang: Language handling configuration.
        text: Text to phonemize.
        arpa_dict: Dictionary mapping words to ARPA phonemes.

    Returns:
        AMIRABET phonetic representation with unknown words marked.

    Side Effects:
        Logs warnings for words not found in the ARPA dictionary.
    """
    normalized_words = normalize(text=text, lang=lang)
    result = []

    for word in normalized_words:
        arpa_phonemes = arpa_dict.get(word.upper())
        if arpa_phonemes is None:
            arpa_phonemes = [_UNKNOWN_WORD_MARKER]
            logger.warning(
                f"Expected word '{word}' not present in ARPA dict - will be treated as a skip"
            )

        amirabet_phonemes = arpa2amirabet_ex(
            lang,
            arpa_phonemes.split() if isinstance(arpa_phonemes, str) else arpa_phonemes,
            pass_through_specials=[_UNKNOWN_WORD_MARKER],
        )
        result.append(amirabet_phonemes)

    return _SPACE_SEPARATOR.join(result)


def _phonemize_text(
    *,
    lang: LanguageHandling,
    text: str,
    arpa_dict: dict[str, list[str]] | None = None,
    target: str = "IPA",
) -> str:
    """Converts text to phonetic representation based on target encoding.

    Args:
        lang: Language handling configuration.
        text: Text to phonemize.
        arpa_dict: Optional dictionary for ARPA/AMIRABET conversion.
        target: Target phonetic encoding ("IPA", "ARPA", or "AMIRABET").

    Returns:
        Phonetic representation in the specified target encoding.

    Raises:
        ValueError: If target encoding is unsupported or required dictionary is missing.
    """
    if target == "IPA" or arpa_dict is None:
        return _phonemize_to_ipa(lang=lang, text=text)
    elif target == "ARPA":
        return _phonemize_to_arpa(lang=lang, text=text, arpa_dict=arpa_dict)
    elif target == "AMIRABET":
        return _phonemize_to_amirabet(lang=lang, text=text, arpa_dict=arpa_dict)
    else:
        raise ValueError(f"Unsupported target encoding: {target}")


def _calculate_wordwise_match(
    *, story_word: str, word1: str, word2: str, method: WordSelectionStrategy
) -> str:
    """Selects the preferred word based on the specified selection method.

    Args:
        story_word: Reference word for comparison.
        word1: First candidate word.
        word2: Second candidate word.
        method: Selection method to use.

    Returns:
        The preferred word based on the selection method.
    """
    if method == WordSelectionStrategy.FIRST:
        return word1
    elif method == WordSelectionStrategy.LEVENSHTEIN_DISTANCE:
        distance1 = l_dist(story_word, s2=word1)
        distance2 = l_dist(story_word, s2=word2)
        return word1 if distance1 <= distance2 else word2
    else:
        raise ValueError(f"Unsupported wordwise selection method: {method}")


def _calculate_groupwise_match(
    *, story_word: str, transcript_words: list[str], grouping_limit: int
) -> str:
    """Finds the best match by combining adjacent words up to the grouping limit.

    This function groups consecutive words from the transcript and compares them
    to the story word using Levenshtein distance and length difference as tie-breaker.

    Args:
        story_word: Reference word to match against.
        transcript_words: List of transcript words to group and compare.
        grouping_limit: Maximum number of words to group together.

    Returns:
        The best matching combination of words.
    """
    if not transcript_words:
        return ""

    best_match = ""
    min_levenshtein_distance = float("inf")
    min_length_difference = float("inf")

    max_group_size = min(len(transcript_words) + 1, grouping_limit + 1)

    for group_size in range(1, max_group_size):
        combined_words = "".join(transcript_words[:group_size])
        levenshtein_distance = l_dist(story_word, s2=combined_words)
        length_difference = abs(len(story_word) - len(combined_words))

        is_better_distance = levenshtein_distance < min_levenshtein_distance
        is_same_distance_better_length = (
            levenshtein_distance == min_levenshtein_distance
            and length_difference < min_length_difference
        )

        if is_better_distance or is_same_distance_better_length:
            min_levenshtein_distance = levenshtein_distance
            min_length_difference = length_difference
            best_match = combined_words

    return best_match


def _calculate_ensemble_match(
    *, story_word: str, transcript_words: list[str], grouping_limit: int
) -> str:
    """Performs ensemble selection comparing groupwise and wordwise matches.

    This function compares the best groupwise match and the best wordwise match
    using fuzz score as primary criterion and Levenshtein distance as tie-breaker.

    Args:
        story_word: Reference word to match against.
        transcript_words: List of transcript words to evaluate.
        grouping_limit: Maximum number of words for groupwise matching.

    Returns:
        The best match from ensemble comparison.
    """
    groupwise_match = _calculate_groupwise_match(
        story_word=story_word,
        transcript_words=transcript_words,
        grouping_limit=grouping_limit,
    )

    wordwise_match = reduce(
        lambda w1, w2: _calculate_wordwise_match(
            story_word=story_word,
            word1=w1,
            word2=w2,
            method=WordSelectionStrategy.LEVENSHTEIN_DISTANCE,
        ),
        transcript_words,
    )

    fuzz_score_group = fuzz.ratio(story_word, groupwise_match)
    fuzz_score_word = fuzz.ratio(story_word, wordwise_match)

    if fuzz_score_group == fuzz_score_word:
        groupwise_distance = l_dist(story_word, s2=groupwise_match)
        wordwise_distance = l_dist(story_word, s2=wordwise_match)
        return groupwise_match if groupwise_distance < wordwise_distance else wordwise_match

    return groupwise_match if fuzz_score_group > fuzz_score_word else wordwise_match


def story_word_match(
    *,
    story_word: str,
    transcript_words: list[str],
    config: AlignmentConfig | None = None,
    grouping_limit: int = 3,
) -> str:
    """Finds the preferred alignment for a story word using configured matching methods.

    Args:
        story_word: The word from the story (expected word).
        transcript_words: List of words from the transcript.
        config: Configuration that determines the selection method.
        grouping_limit: Maximum number of words to group for groupwise matching.

    Returns:
        The best matched word or group of words based on the selected method.

    Raises:
        ValueError: If an unknown word selection method is specified.
    """
    if config is None:
        config = AlignmentConfig.create_default()

    selection_method = config.word_selection_strategy

    if selection_method == WordSelectionStrategy.ENSEMBLE:
        return _calculate_ensemble_match(
            story_word=story_word,
            transcript_words=transcript_words,
            grouping_limit=grouping_limit,
        )
    elif selection_method == WordSelectionStrategy.GROUPWISE:
        return _calculate_groupwise_match(
            story_word=story_word,
            transcript_words=transcript_words,
            grouping_limit=grouping_limit,
        )
    elif selection_method in {
        WordSelectionStrategy.FIRST,
        WordSelectionStrategy.LEVENSHTEIN_DISTANCE,
    }:
        return reduce(
            lambda w1, w2: _calculate_wordwise_match(
                story_word=story_word, word1=w1, word2=w2, method=selection_method
            ),
            transcript_words,
        )
    else:
        raise ValueError(f"Unknown word selection method: {selection_method}")


def _optimize_alignment_contiguity(
    *, codes: list[tuple[str, int, int, int, int]], story_phone: str, transcript_phone: str
) -> None:
    """Optimizes alignment to reduce discontiguities in transcription mapping.

    This function post-processes edit sequences to prefer contiguous matches over
    discontiguous ones when the edit distance is equivalent. It addresses cases where
    edit distance minimization doesn't favor minimizing contiguous runs.

    Args:
        codes: List of edit operation tuples to modify in-place.
        story_phone: Reference phonetic sequence.
        transcript_phone: Transcript phonetic sequence.

    Side Effects:
        Modifies the codes list in-place to optimize contiguity.
    """
    last_was_equal = False
    idx = 0

    while idx < len(codes):
        if last_was_equal and codes[idx][0] == "insert":
            idx2 = idx
            while idx2 < len(codes) and codes[idx2][0] == "insert":
                idx2 += 1

            moved_count = 0
            while (
                idx2 < len(codes)
                and codes[idx2][0] == "equal"
                and story_phone[codes[idx - 1][1] + 1] == transcript_phone[codes[idx][3]]
            ):
                codes[idx] = (
                    "equal",
                    codes[idx - 1][1] + 1,
                    codes[idx - 1][2] + 1,
                    codes[idx - 1][3] + 1,
                    codes[idx - 1][4] + 1,
                )
                codes[idx2] = (
                    "insert",
                    codes[idx2 - 1][2],
                    codes[idx2 - 1][2],
                    codes[idx2 - 1][3] + 1,
                    codes[idx2 - 1][4] + 1,
                )
                idx2 += 1
                idx += 1
                moved_count += 1

            last_was_equal = moved_count > 0
        else:
            last_was_equal = codes[idx][0] == "equal"
            idx += 1


def _build_word_mapping(
    *, codes: list[tuple[str, int, int, int, int]], story_phone: str
) -> tuple[dict[int, int], dict[int, int]]:
    """Builds mapping dictionaries for word start and end positions.

    Args:
        codes: List of edit operation tuples.
        story_phone: Reference phonetic sequence.

    Returns:
        Tuple of (start_mapping, end_mapping) dictionaries.
    """
    story_trans_word_start_map = {}
    story_trans_word_end_map = {}
    last_matched_trans = -1

    for code, ind_story_start, ind_story_end, ind_trans_start, ind_trans_end in codes:
        if ind_story_end > ind_story_start:
            story_trans_word_start_map[ind_story_start] = ind_trans_start

            if code != "insert" and story_phone[ind_story_start] == _SPACE_SEPARATOR:
                last_matched_trans = -1
            elif code in ("equal", "replace"):
                last_matched_trans = ind_trans_end

            story_trans_word_end_map[ind_story_end] = last_matched_trans

    return story_trans_word_start_map, story_trans_word_end_map


def _extract_word_boundaries(*, story_phone: str) -> list[int]:
    """Extracts word boundary positions from phonetic sequence.

    Args:
        story_phone: Phonetic sequence to analyze.

    Returns:
        List of space positions indicating word boundaries.
    """
    space_pattern = re.compile(_SPACE_SEPARATOR)
    return [match.start(0) for match in space_pattern.finditer(story_phone)]


def _process_word_match(
    *,
    story_phone: str,
    transcript_phone: str,
    start: int,
    end: int,
    start_mapping: dict[int, int],
    end_mapping: dict[int, int],
    config: AlignmentConfig,
) -> tuple[str, str, float]:
    """Processes a single word match and calculates alignment metrics.

    Args:
        story_phone: Reference phonetic sequence.
        transcript_phone: Transcript phonetic sequence.
        start: Start position in story phone.
        end: End position in story phone.
        start_mapping: Mapping of story start positions to transcript positions.
        end_mapping: Mapping of story end positions to transcript positions.
        config: Alignment configuration.

    Returns:
        Tuple of (story_word, transcript_word, distance).
    """
    story_word = story_phone[start:end]
    transcript_word_start = start_mapping[start]
    transcript_word_end = end_mapping[end]

    if transcript_word_end - transcript_word_start > 0:
        transcript_segment = transcript_phone[transcript_word_start:transcript_word_end]
        transcript_words = transcript_segment.split()

        if transcript_words:
            transcript_word = story_word_match(
                story_word=story_word, transcript_words=transcript_words, config=config
            )
        else:
            transcript_word = _SKIP_MARKER
    else:
        transcript_word = _SKIP_MARKER

    transcript_word = transcript_word.strip()
    distance = float(l_dist(story_word, s2=transcript_word))

    return story_word, transcript_word, distance


def phoneme_align_dash(
    *,
    story_phone: str,
    transcript_phone: str,
    config: AlignmentConfig | None = None,
) -> tuple[list[int], list[str], list[str], list[float], list[str]]:
    """Performs phoneme-level alignment between story and transcript phonetic sequences.

    This function aligns phonetic sequences using edit distance algorithms and optimizes
    for contiguous matches. It processes word boundaries and calculates alignment metrics.

    Args:
        story_phone: Reference phonetic sequence.
        transcript_phone: Transcript phonetic sequence.
        config: Alignment configuration parameters.

    Returns:
        Tuple containing:
        - story_word_index: Indices of story words
        - story_aligned: Aligned story words
        - transcripts_aligned: Aligned transcript words
        - word_phoneme_distance: Distance metrics for each word pair
        - prefixes: Prefix segments from transcript
    """
    if config is None:
        config = AlignmentConfig.create_default()

    story_phone = story_phone.strip()
    transcript_phone = transcript_phone.strip()

    action_function = (
        edit_distance.highest_match_action
        if config.sequence_match_maximal
        else edit_distance.lowest_cost_action
    )

    sequence_matcher = edit_distance.SequenceMatcher(
        a=story_phone, b=transcript_phone, action_function=action_function
    )
    codes = sequence_matcher.get_opcodes()

    _optimize_alignment_contiguity(
        codes=codes, story_phone=story_phone, transcript_phone=transcript_phone
    )

    start_mapping, end_mapping = _build_word_mapping(codes=codes, story_phone=story_phone)
    space_indices = _extract_word_boundaries(story_phone=story_phone)

    story_aligned = []
    transcripts_aligned = []
    story_word_index = []
    word_phoneme_distance = []
    prefixes = []

    last_end = 0

    for word_idx, space_pos in enumerate(space_indices):
        story_word_index.append(word_idx)

        start = 0 if word_idx == 0 else space_indices[word_idx - 1] + 1
        end = space_pos

        trans_word_start = start_mapping[start]
        prefix = transcript_phone[last_end:trans_word_start] if trans_word_start > last_end else ""

        last_end = start_mapping[end]
        prefixes.append(prefix)

        story_word, transcript_word, distance = _process_word_match(
            story_phone=story_phone,
            transcript_phone=transcript_phone,
            start=start,
            end=end,
            start_mapping=start_mapping,
            end_mapping=end_mapping,
            config=config,
        )

        story_aligned.append(story_word)
        transcripts_aligned.append(transcript_word)
        word_phoneme_distance.append(distance)

    next_index = space_indices[-1] + 1 if space_indices else 0
    trans_word_start = start_mapping[next_index]
    final_prefix = (
        transcript_phone[last_end:trans_word_start] if trans_word_start > last_end else ""
    )
    prefixes.append(final_prefix.strip())

    story_word_index.append(len(space_indices))

    final_story_word, final_transcript_word, final_distance = _process_word_match(
        story_phone=story_phone,
        transcript_phone=transcript_phone,
        start=next_index,
        end=len(story_phone),
        start_mapping=start_mapping,
        end_mapping=end_mapping,
        config=config,
    )

    story_aligned.append(final_story_word)
    transcripts_aligned.append(final_transcript_word)
    word_phoneme_distance.append(final_distance)

    return (
        story_word_index,
        story_aligned,
        transcripts_aligned,
        word_phoneme_distance,
        prefixes,
    )


@curry  # type: ignore[misc]
def align_texts_phonetic(
    *,
    phon_encoding: str | None,
    arpa_dict: dict[str, list[str]] | None,
    config: AlignmentConfig | None,
    lang: LanguageHandling,
    next_exp: str | None,
    ref: str,
    trans: str,
) -> AlignmentResult:
    """Aligns reference and transcript texts using phonetic comparison.

    This function performs phonetic alignment between reference and transcript texts
    using the specified phonetic encoding and language configuration. It handles
    empty transcripts and validates language support.

    Args:
        phon_encoding: Phonetic encoding to use (None, "ARPA", or "AMIRABET").
        arpa_dict: Dictionary for ARPA phoneme lookup.
        config: Alignment configuration parameters.
        lang: Language handling configuration.
        next_exp: Next expected text (unused in current implementation).
        ref: Reference text to align.
        trans: Transcript text to align.

    Returns:
        AlignmentResult tuple containing alignment results.

    Raises:
        ValueError: If the language/encoding combination is not supported.
    """
    if phon_encoding not in _SUPPORTED_LANGUAGES.get(lang, []):
        _validate_language_support(lang=lang)

    if not trans:
        normalized_ref = ref.split()
        ref_length = len(normalized_ref)
        skip_markers = [_SKIP_MARKER] * ref_length
        word_indices = list(range(ref_length))

        return AlignmentResult(
            aligned_reference=normalized_ref,
            aligned_transcription_features=skip_markers,
            reference_word_indices=word_indices,
            transcription_word_indices=word_indices,
            raw_aligned_reference=normalized_ref,
            raw_aligned_transcription_features=skip_markers,
        )

    target_encoding = "IPA" if phon_encoding is None else phon_encoding

    phonetic_ref = _phonemize_text(lang=lang, text=ref, arpa_dict=arpa_dict, target=target_encoding)

    if phon_encoding is None:
        phonetic_trans = _phonemize_text(
            lang=lang,
            text=trans,
            arpa_dict=arpa_dict,
            target=target_encoding,
        )
    elif phon_encoding == "ARPA":
        phonetic_trans = arpa2amirabet_ex(lang, trans.split())
    elif phon_encoding == "AMIRABET":
        phonetic_trans = trans
    else:
        raise ValueError(f"Unsupported phonetic encoding: {phon_encoding}")

    exp_indexes, exp_phons, trans_phons, trans_dists, prefixes = phoneme_align_dash(
        story_phone=phonetic_ref, transcript_phone=phonetic_trans, config=config
    )

    aligned_ref = normalize(text=ref, lang=lang)
    num_words = len(aligned_ref)
    word_indices = list(range(num_words))

    return AlignmentResult(
        aligned_reference=aligned_ref,
        aligned_transcription_features=trans_phons,
        reference_word_indices=word_indices,
        transcription_word_indices=word_indices,
        raw_aligned_reference=aligned_ref,
        raw_aligned_transcription_features=trans_phons,
    )
