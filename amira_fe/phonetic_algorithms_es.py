#!/usr/bin/env python

"""
The Spanish Metaphone Algorithm (Algoritmo del Metáfono para el Español)

This script implements the Metaphone algorithm (c) 1990 by Lawrence Philips.
It was inspired by the English double metaphone algorithm implementation by
Andrew Collins - January 12, 2007 who claims no rights to this work
(http://www.atomodo.com/code/double-metaphone)


The metaphone port adapted to the Spanish Language is authored
by Alejandro Mosquera <amosquera@dlsi.ua.es> November, 2011
and is covered under this copyright:

Copyright 2011, Alejandro Mosquera <amosquera@dlsi.ua.es>.  All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this
list of conditions and the following disclaimer in the documentation and/or
other materials provided with the distribution.


THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from types import MappingProxyType
from typing import Final

_VOWELS: Final[frozenset[str]] = frozenset(["A", "E", "I", "O", "U"])
_SINGLE_SOUND_CONSONANTS: Final[frozenset[str]] = frozenset(
    ["D", "F", "J", "K", "M", "N", "P", "T", "V", "L", "Y"]
)
_SPANISH_CHAR_REPLACEMENTS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "á": "A",
        "ch": "X",
        "ç": "S",
        "é": "E",
        "í": "I",
        "ó": "O",
        "ú": "U",
        "ñ": "NY",
        "gü": "W",
        "ü": "U",
        "b": "V",
        "ll": "Y",
    }
)
_DEFAULT_KEY_LENGTH: Final[int] = 6


class PhoneticAlgorithmsES:
    """Spanish Metaphone algorithm implementation for phonetic matching."""

    @staticmethod
    def string_at(string: str, start: int, string_length: int, patterns: list[str]) -> bool:
        """Check if any pattern from the list is found at the specified position.

        Args:
            string: The input string to search in
            start: Starting position for the search
            string_length: Length of substring to check
            patterns: List of patterns to match against

        Returns:
            True if any pattern is found at the position, False otherwise
        """
        if start < 0 or start >= len(string):
            return False

        for pattern in patterns:
            if string.find(pattern, start, start + string_length) != -1:
                return True
        return False

    @staticmethod
    def substr(string: str, start: int, string_length: int) -> str:
        """Extract substring from the given string.

        Args:
            string: Source string
            start: Starting position
            string_length: Length of substring to extract

        Returns:
            Extracted substring
        """
        return string[start : start + string_length]

    @staticmethod
    def is_vowel(string: str, pos: int) -> bool:
        """Check if character at position is a vowel.

        Args:
            string: Input string
            pos: Position to check

        Returns:
            True if character at position is a vowel, False otherwise
        """
        if pos >= len(string):
            return False
        return string[pos] in _VOWELS

    @staticmethod
    def _normalize_spanish_text(text: str) -> str:
        """Replace Spanish characters with their phonetic equivalents.

        Handles accented characters, digraphs, and phonetically similar letters.
        Examples: 'á' -> 'A', 'ch' -> 'X', 'b' -> 'V', 'll' -> 'Y'

        Args:
            text: Input text to normalize

        Returns:
            Normalized text with Spanish character replacements
        """
        if not text:
            return ""

        normalized = text
        for original, replacement in _SPANISH_CHAR_REPLACEMENTS.items():
            normalized = normalized.replace(original, replacement)
        return normalized

    def _process_consonant_c(self, original_string: str, current_pos: int) -> tuple[str, int]:
        """Process consonant 'C' with Spanish phonetic rules.

        Handles special cases:
        - 'CC' -> 'X' (as in 'acción', 'reacción')
        - 'CE', 'CI' -> 'Z' (as in 'cesar', 'cien', 'cid', 'conciencia')
        - Default -> 'K'

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if self.substr(original_string, current_pos + 1, 1) == "C":
            return "X", current_pos + 2
        elif self.string_at(original_string, current_pos, 2, ["CE", "CI"]):
            return "Z", current_pos + 2
        else:
            return "K", current_pos + 1

    def _process_consonant_g(self, original_string: str, current_pos: int) -> tuple[str, int]:
        """Process consonant 'G' with Spanish phonetic rules.

        Handles special cases:
        - 'GE', 'GI' -> 'J' (as in 'gente', 'ecologia')
        - Default -> 'G'

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if self.string_at(original_string, current_pos, 2, ["GE", "GI"]):
            return "J", current_pos + 2
        else:
            return "G", current_pos + 1

    def _process_consonant_h(self, original_string: str, current_pos: int) -> tuple[str, int]:
        """Process consonant 'H' with Spanish phonetic rules.

        Since 'H' is silent in Spanish, sets the meta key to the vowel
        after the letter 'H' if present, otherwise keeps 'H'.

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if self.is_vowel(original_string, current_pos + 1):
            return original_string[current_pos + 1], current_pos + 2
        else:
            return "H", current_pos + 1

    def _process_consonant_q(self, original_string: str, current_pos: int) -> tuple[str, int]:
        """Process consonant 'Q' with Spanish phonetic rules.

        Handles 'QU' combinations and maps to 'K' sound.

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if self.substr(original_string, current_pos + 1, 1) == "U":
            return "K", current_pos + 2
        else:
            return "K", current_pos + 1

    def _process_consonant_s(self, original_string: str, current_pos: int) -> tuple[str, int]:
        """Process consonant 'S' with Spanish phonetic rules.

        Handles Spanish words starting with 'S' + consonant (like 'spain').

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if not self.is_vowel(original_string, current_pos + 1) and current_pos == 0:
            return "ES", current_pos + 1
        else:
            return "S", current_pos + 1

    def _process_consonant_x(
        self, original_string: str, current_pos: int, string_length: int
    ) -> tuple[str, int]:
        """Process consonant 'X' with Spanish phonetic rules.

        Handles Mexican Spanish words like 'Xochimilco', 'xochitl' and
        words starting with 'X' + consonant.

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string
            string_length: Length of the original string

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if (
            not self.is_vowel(original_string, current_pos + 1)
            and string_length > 1
            and current_pos == 0
        ):
            return "EX", current_pos + 1
        else:
            return "X", current_pos + 1

    def _process_single_sound_consonant(
        self, original_string: str, current_pos: int, current_char: str
    ) -> tuple[str, int]:
        """Process consonants with single sound or already replaced equivalents.

        Handles consonants like 'D', 'F', 'J', 'K', 'M', 'N', 'P', 'T', 'V', 'L', 'Y'
        and manages repeated letters (like 'RR', 'LL').

        Args:
            original_string: The normalized input string
            current_pos: Current position in the string
            current_char: Current character being processed

        Returns:
            Tuple of (phonetic_key, new_position)
        """
        if self.substr(original_string, current_pos + 1, 1) == current_char:
            return current_char, current_pos + 2
        else:
            return current_char, current_pos + 1

    # TODO this is getting deeply nested and is hard to read.
    # TODO consider refactoring this to be more readable.
    def metaphone(self, string: str) -> str:
        """Generate Spanish metaphone key for phonetic matching.

        Implements the Spanish Metaphone algorithm which converts Spanish words
        into phonetic keys that sound similar when pronounced. The algorithm
        handles Spanish-specific phonetic rules including:
        - Vowel handling at string beginning
        - Consonant clusters and digraphs
        - Silent letters (like 'H')
        - Phonetically similar letters ('B'/'V', 'C'/'K'/'Q')
        - Spanish character normalization (accents, ñ, etc.)

        Args:
            string: Input Spanish word to generate metaphone key for

        Returns:
            Phonetic key string (maximum 6 characters)
        """
        meta_key = ""
        current_pos = 0
        original_string = string + ""

        original_string = self._normalize_spanish_text(original_string.lower())
        original_string = original_string.upper()

        while len(meta_key) < _DEFAULT_KEY_LENGTH:
            if current_pos >= len(original_string):
                break

            current_char = original_string[current_pos]

            if self.is_vowel(original_string, current_pos) and current_pos == 0:
                meta_key += current_char
                current_pos += 1
            else:
                if current_char in _SINGLE_SOUND_CONSONANTS:
                    phonetic_key, new_pos = self._process_single_sound_consonant(
                        original_string, current_pos, current_char
                    )
                    meta_key += phonetic_key
                    current_pos = new_pos
                else:
                    match current_char:
                        case "C":
                            phonetic_key, new_pos = self._process_consonant_c(
                                original_string, current_pos
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case "G":
                            phonetic_key, new_pos = self._process_consonant_g(
                                original_string, current_pos
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case "H":
                            phonetic_key, new_pos = self._process_consonant_h(
                                original_string, current_pos
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case "Q":
                            phonetic_key, new_pos = self._process_consonant_q(
                                original_string, current_pos
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case "W":
                            meta_key += "U"
                            current_pos += 1
                        case "R":
                            current_pos += 1
                            meta_key += "R"
                        case "S":
                            phonetic_key, new_pos = self._process_consonant_s(
                                original_string, current_pos
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case "Z":
                            current_pos += 1
                            meta_key += "Z"
                        case "X":
                            phonetic_key, new_pos = self._process_consonant_x(
                                original_string, current_pos, len(string)
                            )
                            meta_key += phonetic_key
                            current_pos = new_pos
                        case _:
                            current_pos += 1

        return meta_key.strip()


if __name__ == "__main__":
    pa = PhoneticAlgorithmsES()
    words = [
        "X",
        "xplosion",
        "escalera",
        "scalera",
        "mi",
        "tu",
        "su",
        "te",
        "ochooomiiiillllllll",
        "complicado",
        "ácaro",
        "ácido",
        "clown",
        "down",
        "col",
        "clon",
        "waterpolo",
        "aquino",
        "rebosar",
        "rebozar",
        "grajea",
        "gragea",
        "encima",
        "enzima",
        "alhamar",
        "abollar",
        "aboyar",
        "huevo",
        "webo",
        "macho",
        "xocolate",
        "chocolate",
        "axioma",
        "abedul",
        "a",
        "gengibre",
        "yema",
        "wHISKY",
        "google",
        "xilófono",
        "web",
        "guerra",
        "pingüino",
        "si",
        "ke",
        "que",
        "tu",
        "gato",
        "gitano",
        "queso",
        "paquete",
        "cuco",
        "perro",
        "pero",
        "arrebato",
        "hola",
        "zapato",
        "españa",
        "garrulo",
        "expansión",
        "membrillo",
        "jamón",
        "risa",
        "caricia",
        "llaves",
        "paella",
        "cerilla",
    ]
    for s in words:
        print(s, " -> ", pa.metaphone(s))
