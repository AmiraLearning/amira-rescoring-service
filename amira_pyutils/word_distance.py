from jellyfish import levenshtein_distance
from typing import Any
import numpy as np
import pandas as pd

from amira_pyutils.logging import get_logger


logger = get_logger(name=__name__)


def l_dist(s1: str, *, s2: str) -> int:
    """Calculate Levenshtein distance between two strings.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Levenshtein distance between the two strings
    """
    try:
        return levenshtein_distance(s1.lower(), s2.lower())
    except (TypeError, AttributeError) as e:
        logger.warning(f"Distance calculation error on inputs '{s1}' and '{s2}': {e}")
        return max(len(str(s1)), len(str(s2)))
    except Exception as e:
        logger.exception(f"Unexpected distance exception on inputs '{s1}' and '{s2}': {e}")
        return max(len(str(s1)), len(str(s2)))


distance = np.vectorize(l_dist)


def min_l_dist_normed(
    s1_candidates: np.ndarray[Any, Any], *, s2: str, treat_first_as_ref: bool = False
) -> float:
    """Calculate minimum normalized Levenshtein distance between a list of strings and a target string.

    Args:
        s1_candidates: List of candidate strings
        s2: Target string
        treat_first_as_ref: Whether to treat the first string as the reference

    Returns:
        Minimum normalized Levenshtein distance between the candidate strings and the target string
    """
    if treat_first_as_ref:
        return min([l_dist(s1, s2=s2) / len(s1) for s1 in s1_candidates])
    else:
        return min([2.0 * l_dist(s1, s2=s2) / (len(s1) + len(s2)) for s1 in s1_candidates])


def letters_dist(s1: str, *, s2: str) -> float:
    """Calculate distance between two strings, treating them as letters.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Distance between the two strings, treating them as letters
    """
    if s1[1:].lower() == "letter":
        s1 = s1[0]
    if s2[1:].lower() == "letter":
        s2 = s2[0]
    if s1[0] != s2[0]:
        return 1
    else:
        if s1[1:] == s2[1:]:
            return 0
        else:
            # Same letter but one is sound and the other is letter
            return 0.5


letters_distance = np.vectorize(letters_dist, otypes=[np.float64])


def missing_trans(strings: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Replace missing or empty strings with spaces.

    Args:
        strings: Array of strings

    Returns:
        Array of strings with missing or empty strings replaced with spaces
    """

    def _normalize_string(*, value: str | None) -> str:
        """Normalize a single string value by replacing missing/empty with space.

        Args:
            value: String value to normalize

        Returns:
            Normalized string (space if missing/empty, original otherwise)
        """
        if pd.isnull(value) or value == "-" or len(str(value)) == 0:
            return " "
        return str(value)

    vectorized_normalize = np.vectorize(_normalize_string, otypes=[str])
    return vectorized_normalize(strings)  # type: ignore[no-any-return]


def word_distance(
    first: np.ndarray[Any, Any],
    *,
    second: np.ndarray[Any, Any],
    treat_first_as_ref: bool = False,
    letter_sounds: bool = False,
) -> np.ndarray[Any, Any]:
    """Calculate word distance between two arrays of strings.

    Args:
        first: First array of strings
        second: Second array of strings
        treat_first_as_ref: Whether to treat the first array as the reference
        letter_sounds: Whether to treat the strings as letter sounds

    Returns:
        Array of word distances between the two arrays of strings
    """

    def _calculate_string_lengths(*, strings: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        """Calculate lengths of strings in array."""
        return np.vectorize(len)(strings)  # type: ignore

    normalized_first = missing_trans(first)
    normalized_second = missing_trans(second)

    if letter_sounds:
        return letters_distance(normalized_first, normalized_second)  # type: ignore[no-any-return]

    raw_distances = distance(normalized_first, normalized_second)

    if treat_first_as_ref:
        first_lengths = _calculate_string_lengths(strings=normalized_first)
        return raw_distances / first_lengths  # type: ignore[no-any-return]

    first_lengths = _calculate_string_lengths(strings=normalized_first)
    second_lengths = _calculate_string_lengths(strings=normalized_second)
    combined_lengths = first_lengths + second_lengths

    both_empty = (normalized_first == " ") & (normalized_second == " ")
    normalized_distances = 2.0 * raw_distances / combined_lengths

    return np.where(both_empty, 1.0, normalized_distances)


def min_word_distance(
    first_candidates: np.ndarray[Any, Any],
    *,
    second: np.ndarray[Any, Any],
    treat_first_as_ref: bool = False,
) -> np.ndarray[Any, Any]:
    """
    Calculate minimum word distance between a list of candidate strings and a target string.

    Args:
        first_candidates: 2D array of strings (array of N candidate lists)
        second: 1D array of N strings
        treat_first_as_ref: Whether to treat the first array as the reference

    Returns:
        Array of minimum word distances between the candidate strings and the target string
    """
    distances: list[float] = []

    for candidate_list, target_string in zip(first_candidates, second):
        normalized_candidates = missing_trans(candidate_list)

        normalized_target = target_string
        if pd.isnull(target_string) or target_string == "-" or len(target_string) == 0:
            normalized_target = " "

        min_distance = min_l_dist_normed(
            normalized_candidates,
            s2=normalized_target,
            treat_first_as_ref=treat_first_as_ref,
        )
        distances.append(min_distance)

    return np.array(distances)
