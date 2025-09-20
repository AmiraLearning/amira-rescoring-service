import re
import unicodedata
from enum import StrEnum
from typing import Final

from amira_pyutils.language import LanguageHandling

MOJIBAKE_SEQUENCE: Final[str] = "â"
WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s\s+")
# Match hashtags like #word (one or more word characters)
HASHTAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"#\w+")
AMPERSAND_PATTERN: Final[re.Pattern[str]] = re.compile(r" &(?= )|^&$|^&(?= )| &$")
LIGHT_STRIP_CHARS: Final[str] = "()?,.!:;'\"\u00bf"


def normalize(*, text: str, lang: LanguageHandling = LanguageHandling.ENGLISH) -> list[str]:
    """Normalize text by cleaning and tokenizing into words.

    Performs comprehensive text normalization including Unicode normalization,
    punctuation removal, and tokenization. Adapted from amira_ml_error_detection.

    Args:
        text: Input text to normalize
        lang: Language handling configuration for normalization rules

    Returns:
        List of normalized word tokens
    """
    cleaned_text = clean(text=text, lang=lang)
    words = HASHTAG_PATTERN.sub(" ", cleaned_text).split()
    return words


def clean(*, text: str, lang: LanguageHandling = LanguageHandling.ENGLISH) -> str:
    """Clean and normalize text for consistent processing.

    Performs comprehensive text cleaning including Unicode normalization,
    case conversion, punctuation removal, and whitespace normalization.
    Adapted from amira_ml_error_detection.

    Args:
        text: Input text to clean
        lang: Language handling configuration for cleaning rules

    Returns:
        Cleaned and normalized text string

    Side Effects:
        None - pure function that only transforms input text
    """
    normalized_text = unicodedata.normalize("NFKC", str(text))

    ampersand_replaced = AMPERSAND_PATTERN.sub(f" {lang.ampersand_replacement}", normalized_text)

    lowercased = ampersand_replaced.lower()

    mojibake_removed = lowercased.replace(MOJIBAKE_SEQUENCE, "")

    punctuation_removed = "".join(
        ch for ch in mojibake_removed if not _is_removable_character(ch=ch)
    )

    whitespace_normalized = WHITESPACE_PATTERN.sub(" ", punctuation_removed).strip()

    return whitespace_normalized


def light_txt_norm(*, text: str) -> list[str]:
    """Apply light normalization suitable for story text canonicalization.

    Performs minimal normalization that preserves Unicode character encoding,
    hyphenated words, and possessives with apostrophes for WordDB lookup
    compatibility.

    Args:
        text: Input text to lightly normalize

    Returns:
        List of lightly normalized word tokens with preserved structure

    Side Effects:
        None - pure function that only transforms input text
    """
    stripped_words = [word.strip(LIGHT_STRIP_CHARS) for word in text.split() if word != "-"]
    return [word for word in stripped_words if len(word) > 0]


class RemovableChar(StrEnum):
    P = "P"  # punctuation
    S = "S"  # symbol
    C = "C"  # control character


def _is_removable_character(*, ch: str) -> bool:
    """Check if a character should be removed during text cleaning.

    Args:
        ch: Single character to evaluate

    Returns:
        True if character should be removed (punctuation, symbol, or control)
    """
    category = unicodedata.category(ch)
    return category[0] in [e.value for e in RemovableChar]
