from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import reduce
from types import MappingProxyType
from typing import Any, Final, cast

from toolz import curry

from amira_pyutils.errors import (
    BadAmirabetError,
    UnsupportedLanguageConversionError,
)
from amira_pyutils.functional import fmap_opt
from amira_pyutils.language import LanguageHandling
from amira_pyutils.logging import get_logger
from amira_pyutils.mappings import (
    ARPA_REPLACEMENTS,
    ARPA_TO_AMIRABET,
    ARPA_TO_AMIRABET_ES_MX_ONLY,
    ARPA_TO_IPA_OVERRIDES,
    ARPA_TO_IPA_OVERRIDES_ES_MX,
    IPA_EMPHASIS_MARKER,
    IPA_EMPHASIS_MARKERS,
    IPA_EQUIV,
    IPA_LIST_CONFLICTING_SYMBOLS,
    IPA_LIST_CONFLICTING_SYMBOLS_ES_MX,
    IPA_MAP_EN_US_DOUBLE,
    IPA_MAP_EN_US_SINGLE,
    IPA_MAP_ES_DOUBLE,
    IPA_MAP_ES_SINGLE,
    IPA_MAP_INCORRECT_DOUBLE,
    IPA_MAP_INCORRECT_DOUBLE_ES_MX,
    IPA_MAP_INCORRECT_SINGLE,
    IPA_MAP_INCORRECT_SINGLE_ES_MX,
    A,
    AmirabetStr,
    ArpaStr,
    IpaStr,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class LanguageParameters:
    """Language-specific phonetic mapping parameters.

    Attributes:
        ipa_map_incorrect_single: Single character mappings
        ipa_map_incorrect_double: Double character mappings
        ipa_conflicting: Conflicting IPA phonemes
        ipa_arpa_map_single: Single character ARPA mappings
        ipa_arpa_map_double: Double character ARPA mappings
        arpa_amira_map: ARPA to AmiraBet mappings
        arpa_to_ipa_overrides: ARPA to IPA overrides
        arpa_replacements: ARPA replacements
    """

    ipa_map_incorrect_single: MappingProxyType[IpaStr, IpaStr]
    ipa_map_incorrect_double: MappingProxyType[IpaStr, IpaStr]
    ipa_conflicting: tuple[IpaStr, ...]
    ipa_arpa_map_single: MappingProxyType[IpaStr, ArpaStr]
    ipa_arpa_map_double: MappingProxyType[IpaStr, ArpaStr]
    arpa_amira_map: MappingProxyType[ArpaStr, AmirabetStr]
    arpa_to_ipa_overrides: MappingProxyType[ArpaStr, IpaStr] | None = None
    arpa_replacements: dict[ArpaStr, ArpaStr] | None = None

    def __post_init__(self) -> None:
        """Initialize derived mappings after construction."""
        combined_ipa_arpa_map: dict[IpaStr, ArpaStr] = {
            **self.ipa_arpa_map_double,
            **self.ipa_arpa_map_single,
        }
        overrides = dict(self.arpa_to_ipa_overrides) if self.arpa_to_ipa_overrides else {}
        arpa_ipa_map = {v: overrides.get(v, k) for k, v in combined_ipa_arpa_map.items() if v != ""}

        object.__setattr__(
            self,
            "_derived_mappings",
            {
                "arpa_ipa_map": self._transform_arpa_mapping(
                    extras=self.arpa_replacements, mapping=arpa_ipa_map.copy()
                ),
                "amira_arpa_map": {v: k for k, v in self.arpa_amira_map.items()},
                "ipa_equivs": self._ipa_equivalencies(
                    combined_ipa_arpa_map=combined_ipa_arpa_map,
                    arpa_ipa_map=arpa_ipa_map,
                ),
            },
        )

        object.__setattr__(
            self,
            "arpa_amira_map",
            self._transform_arpa_mapping(
                extras=self.arpa_replacements, mapping=self.arpa_amira_map.copy()
            ),
        )

    def get_mapping(self, *, name: str) -> dict[Any, Any] | list[Any]:
        """Get mapping by name from parameters or derived mappings.

        Args:
            name: Name of mapping to get

        Returns:
            Mapping or list of mappings
        """
        if hasattr(self, name):
            return cast(dict[Any, Any] | list[Any], getattr(self, name))
        return cast(dict[Any, Any] | list[Any], getattr(self, "_derived_mappings")[name])

    @staticmethod
    def _transform_arpa_mapping(
        *, extras: dict[ArpaStr, ArpaStr] | None, mapping: dict[ArpaStr, A]
    ) -> dict[ArpaStr, A]:
        """Transform ARPA mapping with additional replacements.

        Args:
            extras: Additional replacements
            mapping: Mapping to transform

        Returns:
            Transformed mapping
        """
        for k, v in (extras or {}).items():
            mapping[k] = mapping[v]
        return mapping

    @staticmethod
    def _ipa_equivalencies(
        *,
        combined_ipa_arpa_map: dict[IpaStr, ArpaStr],
        arpa_ipa_map: dict[ArpaStr, IpaStr],
    ) -> list[tuple[str, list[str]]]:
        """Generate IPA equivalency mappings.

        Args:
            combined_ipa_arpa_map: Combined IPA to ARPA mapping
            arpa_ipa_map: ARPA to IPA mapping

        Returns:
            IPA equivalency mappings
        """
        counts = defaultdict(list)
        for k, v in sorted(combined_ipa_arpa_map.items(), key=lambda x: x[0]):
            counts[v].append((k, k == arpa_ipa_map.get(v)))
        return [
            (v, [e[0] for e in sorted(c, key=lambda x: 0 if x[1] else 1)])
            for v, c in counts.items()
            if len(c) > 1
        ]


# TODO should probalby use MappingPRoxyType for these return types
def _create_arpa_to_amirabet_es_mx() -> dict[ArpaStr, AmirabetStr]:
    """Create Spanish-Mexican ARPA to AmiraBet mapping.

    Returns:
        Spanish-Mexican ARPA to AmiraBet mapping
    """
    return {
        k: v for k, v in ARPA_TO_AMIRABET.items() if v not in ARPA_TO_AMIRABET_ES_MX_ONLY.values()
    } | ARPA_TO_AMIRABET_ES_MX_ONLY


EN_US_PARAMS: Final[LanguageParameters] = LanguageParameters(
    ipa_map_incorrect_single=IPA_MAP_INCORRECT_SINGLE,
    ipa_map_incorrect_double=IPA_MAP_INCORRECT_DOUBLE,
    ipa_conflicting=IPA_LIST_CONFLICTING_SYMBOLS,
    ipa_arpa_map_single=IPA_MAP_EN_US_SINGLE,
    ipa_arpa_map_double=IPA_MAP_EN_US_DOUBLE,
    arpa_amira_map=ARPA_TO_AMIRABET,
    arpa_to_ipa_overrides=ARPA_TO_IPA_OVERRIDES,
    arpa_replacements=ARPA_REPLACEMENTS,
)

ES_MX_PARAMS: Final[LanguageParameters] = LanguageParameters(
    ipa_map_incorrect_single=IPA_MAP_INCORRECT_SINGLE_ES_MX,
    ipa_map_incorrect_double=IPA_MAP_INCORRECT_DOUBLE_ES_MX,
    ipa_conflicting=IPA_LIST_CONFLICTING_SYMBOLS_ES_MX,
    ipa_arpa_map_single=IPA_MAP_ES_SINGLE,
    ipa_arpa_map_double=IPA_MAP_ES_DOUBLE,
    arpa_amira_map=MappingProxyType(_create_arpa_to_amirabet_es_mx()),
    arpa_to_ipa_overrides=ARPA_TO_IPA_OVERRIDES_ES_MX,
    arpa_replacements=None,
)

LANGUAGE_PARAMETERS: Final[dict[str, LanguageParameters]] = {
    "en_US": EN_US_PARAMS,
    "es_MX": ES_MX_PARAMS,
}

IPA_NORM_TRANS: Final[dict[int, str]] = str.maketrans(dict(IPA_EQUIV))


def norm_ipa(*, s: IpaStr) -> IpaStr:
    """Normalize IPA string using standard equivalencies.

    Args:
        s: IPA string to normalize

    Returns:
        Normalized IPA string
    """
    return s.translate(IPA_NORM_TRANS)


def _get_language_parameters(*, lang: LanguageHandling) -> LanguageParameters:
    """Get language parameters with validation.

    Args:
        lang: Language to get parameters for

    Returns:
        Language parameters
    """
    if lang.language_code not in LANGUAGE_PARAMETERS:
        raise UnsupportedLanguageConversionError(
            requested_language=lang, supported_codes=list(LANGUAGE_PARAMETERS.keys())
        )
    return LANGUAGE_PARAMETERS[lang.language_code]


def _get_mapping(*, lang: LanguageHandling, name: str) -> dict[str, str] | list[str]:
    """Get specific mapping for language.

    Args:
        lang: Language to get mapping for
        name: Name of mapping to get

    Returns:
        Mapping
    """
    params = _get_language_parameters(lang=lang)
    return params.get_mapping(name=name)


def arpa_phons(*, lang: LanguageHandling) -> frozenset[ArpaStr]:
    """Get all ARPA phonemes for language.

    Args:
        lang: Language to get phonemes for

    Returns:
        Set of ARPA phonemes
    """
    params = _get_language_parameters(lang=lang)
    single_phons = frozenset(params.ipa_arpa_map_single.values())
    double_phons = frozenset(params.ipa_arpa_map_double.values())
    return (single_phons | double_phons) - {""}


@curry  # type: ignore[misc]
def canonicalize_ipa_equivalences(*, lang: LanguageHandling, word: IpaStr) -> IpaStr:
    """Canonicalize IPA equivalences for consistent representation.

    Args:
        lang: Language to canonicalize for
        word: IPA string to canonicalize

    Returns:
        Canonicalized IPA string
    """
    emphasis_indices = [idx for idx, c in enumerate(word) if c in IPA_EMPHASIS_MARKERS]
    mapping = _get_mapping(lang=lang, name="ipa_equivs")
    equivalencies = dict(mapping) if isinstance(mapping, dict) else {}

    def replace_alts(*, w: IpaStr, alts: Sequence[IpaStr]) -> IpaStr:
        return reduce(
            lambda x, a: (
                x.replace(a, alts[0]) if len(a) == 1 else x[0] + x[1:].replace(a, alts[0])
            ),
            alts[1:],
            w,
        )

    result_no_emphasis = reduce(
        lambda w, alts: replace_alts(w=w, alts=alts),
        equivalencies.values(),
        strip_emphasis(phons=word),
    )
    return reduce(
        lambda w, i: w[:i] + IPA_EMPHASIS_MARKER + w[i:],
        emphasis_indices,
        result_no_emphasis,
    )


def _process_ipa_corrections(
    *,
    ipa: IpaStr,
    incorrect_single: MappingProxyType[IpaStr, IpaStr],
    incorrect_double: MappingProxyType[IpaStr, IpaStr],
) -> IpaStr:
    """Apply IPA corrections for single and double character mappings.

    Args:
        ipa: IPA string to correct
        incorrect_single: Single character mappings
        incorrect_double: Double character mappings

    Returns:
        Corrected IPA string
    """
    corrected = ""
    remaining = ipa

    while remaining:
        if remaining[0] in incorrect_single:
            corrected += incorrect_single[remaining[0]]
        else:
            corrected += remaining[0]
        remaining = remaining[1:]

    final_result = ""
    remaining = corrected

    while remaining:
        if len(remaining) >= 2 and remaining[:2] in incorrect_double:
            final_result += incorrect_double[remaining[:2]]
            remaining = remaining[2:]
        else:
            final_result += remaining[0]
            remaining = remaining[1:]

    return final_result


def _map_ipa_to_arpa(
    *,
    ipa: IpaStr,
    single_map: MappingProxyType[IpaStr, ArpaStr],
    double_map: MappingProxyType[IpaStr, ArpaStr],
    conflicting: tuple[IpaStr, ...],
) -> list[ArpaStr]:
    """Map corrected IPA to ARPA phonemes.

    Args:
        ipa: IPA string to map
        single_map: Single character mappings
        double_map: Double character mappings
        conflicting: Conflicting IPA phonemes
    """
    result: list[ArpaStr] = []
    remaining = ipa

    while remaining:
        if (
            len(remaining) >= 2
            and remaining[:2] in double_map
            and (len(result) > 0 or remaining[0] != "w")
        ):
            result.append(double_map[remaining[:2]])
            remaining = remaining[2:]
        else:
            if remaining[0] in single_map:
                result.append(single_map[remaining[0]])
            elif remaining[0] in conflicting:
                logger.error(f"Provided unmappable non-IPA phon {remaining[0]} unknown, omitting")
            else:
                logger.warning(f"Provided IPA phon {remaining[0]} unknown, omitting")
            remaining = remaining[1:]

    return result


@curry  # type: ignore[misc]
def ipa2arpa(*, lang: LanguageHandling, ipa: IpaStr) -> list[ArpaStr]:
    """Convert IPA string to ARPA phoneme list.

    Args:
        lang: Language to convert for
        ipa: IPA string to convert

    Returns:
        List of ARPA phonemes
    """
    params = _get_language_parameters(lang=lang)
    cleaned_ipa = ipa.replace("ˈ", "").replace("'", "")

    if "/" in cleaned_ipa:
        return reduce(
            lambda x, y: x + y,
            [ipa2arpa(lang=lang, ipa=segment) for segment in cleaned_ipa.split("/")],
        )

    corrected_ipa = _process_ipa_corrections(
        ipa=cleaned_ipa,
        incorrect_single=params.ipa_map_incorrect_single,
        incorrect_double=params.ipa_map_incorrect_double,
    )

    return _map_ipa_to_arpa(
        ipa=corrected_ipa,
        single_map=params.ipa_arpa_map_single,
        double_map=params.ipa_arpa_map_double,
        conflicting=params.ipa_conflicting,
    )


def _translate_phoneme(
    *,
    phon: ArpaStr,
    mapping: dict[ArpaStr, str],
    word: str,
    pass_through_specials: list[ArpaStr],
) -> str:
    """Translate single phoneme using mapping with fallback handling.

    Args:
        phon: Phoneme to translate
        mapping: Mapping to use
        word: Word to translate
        pass_through_specials: Special phonemes to pass through
    """
    result = mapping.get(phon)
    if result is None:
        if phon in pass_through_specials:
            return phon
        logger.error(
            f"Unmappable ARPABET token {phon} occurred in {' '.join(word)} - eliding from translation"
        )
        return " "
    return result


@curry  # type: ignore[misc]
def arpa2ipa(
    *,
    lang: LanguageHandling,
    word: list[ArpaStr],
    pass_through_specials: Iterable[ArpaStr] = (),
) -> IpaStr:
    """Convert ARPA phoneme list to IPA string.

    Args:
        lang: Language to convert for
        word: List of ARPA phonemes to convert
        pass_through_specials: Special phonemes to pass through

    Returns:
        IPA string
    """
    arpa_ipa_map = cast(dict[str, str], _get_mapping(lang=lang, name="arpa_ipa_map"))
    pass_through_list = list(pass_through_specials)
    word_str = " ".join(word)

    return "".join(
        _translate_phoneme(
            phon=p,
            mapping=arpa_ipa_map,
            word=word_str,
            pass_through_specials=pass_through_list,
        )
        for p in word
    )


@curry  # type: ignore[misc]
def amirabet2arpa_ex(*, lang: LanguageHandling, word: AmirabetStr) -> list[ArpaStr]:
    """Convert AmiraBet string to ARPA phoneme list.

    Args:
        lang: Language to convert for
        word: AmiraBet string to convert

    Returns:
        List of ARPA phonemes
    """
    amira_arpa_map = cast(dict[str, str], _get_mapping(lang=lang, name="amira_arpa_map"))
    result: list[ArpaStr] = []

    for c in word:
        arpa_c = amira_arpa_map.get(c)
        if arpa_c is None:
            raise BadAmirabetError(invalid_string=word)
        result.append(arpa_c)

    return result


@curry  # type: ignore[misc]
def amirabet2ipa_ex(*, lang: LanguageHandling, amirabet: AmirabetStr) -> IpaStr:
    """Convert AmiraBet string to IPA string.

    Args:
        lang: Language to convert for
        amirabet: AmiraBet string to convert

    Returns:
        IPA string
    """
    word = amirabet2arpa_ex(lang=lang, word=amirabet)
    return cast(str, arpa2ipa(lang=lang, word=word))


@curry  # type: ignore[misc]
def arpa2amirabet_ex(
    *,
    lang: LanguageHandling,
    word: list[ArpaStr],
    pass_through_specials: Iterable[ArpaStr] = (),
) -> AmirabetStr:
    """Convert ARPA phoneme list to AmiraBet string.

    Args:
        lang: Language to convert for
        word: List of ARPA phonemes to convert
        pass_through_specials: Special phonemes to pass through

    Returns:
        AmiraBet string
    """
    arpa_map = cast(dict[str, str], _get_mapping(lang=lang, name="arpa_amira_map"))
    pass_through_list = list(pass_through_specials)
    word_str = " ".join(word)

    return "".join(
        _translate_phoneme(
            phon=p,
            mapping=arpa_map,
            word=word_str,
            pass_through_specials=pass_through_list,
        )
        for p in word
    )


@curry  # type: ignore[misc]
def ipa2amirabet(*, lang: LanguageHandling, ipa: IpaStr) -> AmirabetStr:
    """Convert IPA string to AmiraBet string.

    Args:
        lang: Language to convert for
        ipa: IPA string to convert

    Returns:
        AmiraBet string
    """
    return cast(str, arpa2amirabet_ex(lang=lang, word=ipa2arpa(lang=lang, ipa=ipa)))


@curry  # type: ignore[misc]
def english2amirabet(
    *, text_to_phons: dict[str, ArpaStr], word: str, all_variants: bool = False
) -> AmirabetStr | list[AmirabetStr] | None:
    """Convert English text to AmiraBet using pronunciation dictionary.

    Args:
        text_to_phons: Dictionary of text to phonemes
        word: Word to convert
        all_variants: Whether to return all variants

    Returns:
        AmiraBet string or list of AmiraBet strings
    """
    if not all_variants:
        return cast(
            AmirabetStr | None,
            fmap_opt(
                func=lambda phons: arpa2amirabet_ex(lang=LanguageHandling.ENGLISH, word=phons),
                value=text_to_phons.get(word.upper(), "").split() or None,
            ),
        )

    all_pronunciations = [english2amirabet(text_to_phons=text_to_phons, word=word)]
    variant_index = 1

    while word.upper() + str(variant_index) in text_to_phons:
        all_pronunciations.append(
            english2amirabet(text_to_phons=text_to_phons, word=word + str(variant_index))
        )
        variant_index += 1

    return all_pronunciations


def _normalize_word_for_lookup(*, word: str) -> str:
    """Normalize word for pronunciation dictionary lookup.

    Args:
        word: Word to normalize

    Returns:
        Normalized word
    """
    normalized = word.upper()

    if "_LETTER" in normalized:
        normalized = normalized.replace("_LETTER", "LETTER")
    elif "_SOUND" in normalized:
        normalized = normalized.replace("_SOUND", "SOUND")

    return normalized


def _try_phoneme_fallback(*, word: str, text_to_phons: dict[str, ArpaStr]) -> list[ArpaStr]:
    """Try phoneme-specific fallback lookup.

    Args:
        word: Word to try fallback lookup for
        text_to_phons: Dictionary of text to phonemes

    Returns:
        List of ARPA phonemes
    """
    if word.upper().endswith("PHON"):
        fallback_key = word.upper()[:-4] + "_PHON"
        phons = text_to_phons.get(fallback_key, "").split()
        if phons:
            logger.debug("Adding underscore before PHON to query the right entry")
            return phons
    return []


@curry  # type: ignore[misc]
def text2arpa(
    *,
    text_to_phons: dict[str, ArpaStr],
    text_to_phons_fallback: Callable[..., Any] | None,
    word: str,
    **fallback_kwargs: dict[str, Any],
) -> list[ArpaStr]:
    """Convert text to ARPA phonemes using dictionary with fallback.

    Args:
        text_to_phons: Dictionary of text to phonemes
        text_to_phons_fallback: Fallback function
        word: Word to convert
        **fallback_kwargs: Additional fallback kwargs

    Returns:
        List of ARPA phonemes
    """
    if fallback_kwargs.get("is_amirabet", False):
        if text_to_phons_fallback is None:
            raise ValueError("text_to_phons_fallback cannot be None when is_amirabet is True")
        result = text_to_phons_fallback(word, **fallback_kwargs)
        return list(result) if result is not None else []

    normalized_word = _normalize_word_for_lookup(word=word)
    phons = text_to_phons.get(normalized_word, "").split()

    if not phons:
        phons = _try_phoneme_fallback(word=word, text_to_phons=text_to_phons)

    if not phons and word not in ("xxxx", "-"):
        if text_to_phons_fallback is None:
            logger.error(f"Word {word} has no phoneme mapping - setting null sequence")
        else:
            phons = text_to_phons_fallback(word, **fallback_kwargs)
            logger.debug(
                f"Word {word} has no phoneme mapping - approximated to {phons} with fallback"
            )

    return phons


def strip_syllablization(*, phons: IpaStr) -> IpaStr:
    """Remove syllable markers from IPA string.

    Args:
        phons: IPA string to strip syllable markers from

    Returns:
        IPA string with syllable markers removed
    """
    return phons.translate(str.maketrans("", "", "'/"))


def strip_emphasis_and_syllablization(*, phons: IpaStr) -> IpaStr:
    """Remove emphasis and syllable markers from IPA string.

    Args:
        phons: IPA string to strip emphasis and syllable markers from

    Returns:
        IPA string with emphasis and syllable markers removed
    """
    return phons.translate(str.maketrans("", "", "/" + IPA_EMPHASIS_MARKERS))


def strip_emphasis(*, phons: IpaStr) -> IpaStr:
    """Remove emphasis markers from IPA string.

    Args:
        phons: IPA string to strip emphasis markers from

    Returns:
        IPA string with emphasis markers removed
    """
    return phons.translate(str.maketrans("", "", IPA_EMPHASIS_MARKERS))
