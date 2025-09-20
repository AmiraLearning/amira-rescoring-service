"""
Copyright (c) 2015, James Turk
Copyright (c) 2015, Sunlight Foundation
Copyright (c) 2021, Amira Learning Inc.

All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from types import MappingProxyType
from typing import Final

_DOUBLE_LETTER_REPLACEMENTS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "RR": "R",
        "LL": "L",
    }
)

_PHONETIC_MAPPINGS: Final[tuple[tuple[str, str], ...]] = (
    ("P", "0"),
    ("BV", "1"),
    ("FH", "2"),
    ("TD", "3"),
    ("SZCX", "4"),
    ("YL", "5"),
    ("NÑM", "6"),
    ("QK", "7"),
    ("GJ", "8"),
    ("R", "9"),
)


def spanish_soundex(*, s: str) -> str | None:
    """Encode a Spanish word using a modified Soundex algorithm.

    Adapted from jellyfish's soundex function with modifications based on research
    papers for Spanish phonetic encoding. Unlike traditional Soundex, this returns
    a variable-length string of numbers rather than a fixed-length alphanumeric code.

    The algorithm handles Spanish-specific phonetic patterns:
    - Double letters (RR, LL) are normalized to single letters
    - Phonetically similar letters are grouped and encoded with the same digit
    - Returns numeric encoding rather than traditional Soundex format

    Based on research from:
    - https://www.scitepress.org/Papers/2016/62277/62277.pdf
    - https://www.researchgate.net/publication/285589803_Comparison_of_a_Modified_Spanish_Phonetic_Soundex_and_Phonex_coding_functions_during_data_matching_process
    - http://www.scielo.org.co/pdf/rium/v11n20/v11n20a11.pdf

    Args:
        s: Spanish word to encode. Should be normalized before calling.

    Returns:
        Phonetic encoding as a string of numbers, or None if word cannot be encoded.
    """
    normalized_word = s.upper()

    for double_letter, replacement in _DOUBLE_LETTER_REPLACEMENTS.items():
        if double_letter in normalized_word:
            normalized_word = normalized_word.replace(double_letter, replacement)

    result: list[str] = []

    for letter in normalized_word:
        for letter_set, digit_code in _PHONETIC_MAPPINGS:
            if letter in letter_set:
                result.append(digit_code)
                break

    return "".join(result) if result else None
