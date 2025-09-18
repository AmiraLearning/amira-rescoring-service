from collections.abc import Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from amira_pyutils.jsonable import (
    JsonSerializable,
    JsonSerializer,
)

LANGUAGE_CODES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "ENGLISH": "en_US",
        "SPANISH": "es_MX",
    }
)

AMPERSAND_REPLACEMENTS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "SPANISH_SOUNDEX": "y",
        "SPANISH_METAPHONE": "y",
    }
)

DEFAULT_AMPERSAND_REPLACEMENT: Final[str] = "and"


class _LanguageSerializer(JsonSerializer["LanguageHandling"]):
    """Serializer for LanguageHandling enum values."""

    @classmethod
    def deserialize(cls, json: JsonSerializable) -> "LanguageHandling":
        """Deserialize JSON string to LanguageHandling enum.

        Args:
            json: JSON string representation of language handling variant

        Returns:
            LanguageHandling enum instance

        Raises:
            ValueError: If the language handling variant is not recognized
        """
        if not isinstance(json, str):
            raise ValueError(f"Expected string for language handling, got {type(json)}")
        return LanguageHandling.from_string(name=json)

    @classmethod
    def serialize(cls, value: "LanguageHandling") -> JsonSerializable:
        """Serialize LanguageHandling enum to JSON string.

        Args:
            value: LanguageHandling enum instance to serialize

        Returns:
            String representation of the enum value
        """
        return value.name


class LanguageHandling(StrEnum):
    """Language handling variants supported by the text processing system.

    This enum defines the various language processing modes available,
    including phonetic matching algorithms like metaphone and soundex
    for different languages.
    """

    ENGLISH = "English"
    ENGLISH_METAPHONE = "English_metaphone"
    SPANISH_METAPHONE = "Spanish_metaphone"
    SPANISH_SOUNDEX = "Spanish_soundex"

    @classmethod
    def supported_variants(cls) -> Sequence[str]:
        """Get all supported language handling variant names.

        Returns:
            Sequence of all supported variant names
        """
        return tuple(variant.value for variant in cls)

    @classmethod
    def from_string(cls, *, name: str) -> "LanguageHandling":
        """Create LanguageHandling instance from string name.

        Args:
            name: String name of the language handling variant (case-insensitive)

        Returns:
            LanguageHandling enum instance

        Raises:
            ValueError: If the variant name is not recognized
        """
        normalized_name = name.upper()

        for variant in cls:
            if variant.name == normalized_name:
                return variant

        supported = ", ".join(cls.supported_variants())
        raise ValueError(
            f"Unknown language handling variant '{name}'. Supported variants: {supported}"
        )

    @property
    def ampersand_replacement(self) -> str:
        """Get the appropriate ampersand replacement for this language variant.

        Different languages use different conjunctions. Spanish variants
        use "y" while English variants use "and".

        Returns:
            String to use as ampersand replacement
        """
        return AMPERSAND_REPLACEMENTS.get(self.name, DEFAULT_AMPERSAND_REPLACEMENT)

    @property
    def language_code(self) -> str:
        """Get the ISO language code for this variant.

        Returns:
            ISO language code (e.g., "en_US", "es_MX")

        Raises:
            ValueError: If no language code is defined for this variant
        """
        for language_prefix, code in LANGUAGE_CODES.items():
            if self.name.startswith(language_prefix):
                return code

        raise ValueError(f"Language variant '{self.name}' has no defined language code")

    @classmethod
    def serializer(cls) -> JsonSerializer["LanguageHandling"]:
        """Get the JSON serializer for this enum.

        Returns:
            JsonSerializer instance for LanguageHandling
        """
        return _LanguageSerializer()
