from types import MappingProxyType
from typing import Final, TypeVar

A = TypeVar("A")

IpaStr = str
ArpaStr = str
AmirabetStr = str

IPA_EMPHASIS_MARKER: Final[str] = chr(712)
IPA_EMPHASIS_MARKERS: Final[str] = "'" + IPA_EMPHASIS_MARKER
RARE_MAPPING_ARM: Final[float] = 6.0

ARPA_TO_AMIRABET: Final[MappingProxyType[ArpaStr, AmirabetStr]] = MappingProxyType(
    {
        "SIL": "",
        "AA": "ɑ",
        "AE": "æ",
        "AH": "ʌ",
        "AO": "ɔ",
        "AW": "α",
        "AY": "γ",
        "B": "b",
        "CH": "x",
        "D": "d",
        "DH": "ð",
        "EH": "ɛ",
        "ER": "ɝ",
        "EY": "a",
        "F": "f",
        "G": "g",
        "HH": "h",
        "IH": "ɪ",
        "IY": "i",
        "JH": "j",
        "K": "k",
        "L": "l",
        "M": "m",
        "N": "n",
        "NG": "ŋ",
        "OW": "o",
        "OY": "ω",
        "P": "p",
        "R": "ɹ",
        "S": "s",
        "SH": "ʃ",
        "T": "t",
        "TH": "θ",
        "UH": "ʊ",
        "UW": "u",
        "V": "v",
        "W": "w",
        "Y": "y",
        "Z": "z",
        "ZH": "ʒ",
    }
)

ARPA_REPLACEMENTS: Final[dict[ArpaStr, ArpaStr]] = {"AX": "AH", "AXR": "ER"}

IPA_MAP_EN_US_DOUBLE: Final[MappingProxyType[IpaStr, ArpaStr]] = MappingProxyType(
    {
        "aʊ": "AW",
        "aɪ": "AY",
        "eɪ": "EY",
        "oʊ": "OW",
        "ɔɪ": "OY",
        "ɹ̩": "ER",
    }
)

IPA_MAP_EN_US_SINGLE: Final[MappingProxyType[IpaStr, ArpaStr]] = MappingProxyType(
    {
        "a": "AA",
        "ɑ": "AA",
        "ɒ": "AA",
        "æ": "AE",
        "ʌ": "AH",
        "ə": "AH",
        "ɔ": "AO",
        "o": "AO",
        "b": "B",
        "ʧ": "CH",
        "d": "D",
        "ð": "DH",
        "ɛ": "EH",
        "e": "EH",
        "ɝ": "ER",
        "ɚ": "ER",
        "f": "F",
        "ɡ": "G",
        "g": "G",
        "h": "HH",
        "ɪ": "IH",
        "i": "IY",
        "ʤ": "JH",
        "k": "K",
        "l": "L",
        "ɫ": "L",
        "m": "M",
        "n": "N",
        "ŋ": "NG",
        "p": "P",
        "ɹ": "R",
        "s": "S",
        "ʃ": "SH",
        "t": "T",
        "θ": "TH",
        "ʊ": "UH",
        "u": "UW",
        "v": "V",
        "w": "W",
        "j": "Y",
        "z": "Z",
        "ʒ": "ZH",
        "̩": "",
        "ː": "",
    }
)

IPA_MAP_INCORRECT_SINGLE: Final[MappingProxyType[IpaStr, IpaStr]] = MappingProxyType(
    {
        "I": "ɪ",
        "Z": "ʒ",
        "S": "ʃ",
        "D": "ð",
        "T": "θ",
        "N": "n",
        "V": "ʊ",
        "U": "ʊ",
        "r": "ɹ",
        "c": "ʧ",
        "3": "ɝ",
        "9": "ŋ",
        "&": "æ",
        "'": "ˈ",
    }
)

IPA_MAP_INCORRECT_DOUBLE: Final[MappingProxyType[IpaStr, IpaStr]] = MappingProxyType(
    {
        "dʒ": "ʤ",
        "tʃ": "ʧ",
        "aw": "aʊ",
        "aj": "aɪ",
        "ej": "eɪ",
        "ow": "oʊ",
        "oj": "ɔɪ",
    }
)

ARPA_TO_IPA_OVERRIDES: Final[MappingProxyType[ArpaStr, IpaStr]] = MappingProxyType(
    {
        "AA": "ɑ",
        "AH": "ʌ",
        "AO": "ɔ",
        "EH": "ɛ",
        "ER": "ɝ",
        "G": "g",
        "L": "l",
    }
)

IPA_LIST_CONFLICTING_SYMBOLS: Final[tuple[IpaStr, ...]] = (
    "0",
    "@",
    "A",
    "O",
    "L",
)

IPA_MAP_ES_DOUBLE: Final[MappingProxyType[IpaStr, ArpaStr]] = MappingProxyType(
    {
        "ai": "AI",
        "au": "AU",
        "ei": "EI",
        "eu": "EU",
        "ia": "IA",
        "ie": "IE",
        "io": "IO",
        "iu": "IU",
        "oi": "OI",
        "ua": "UA",
        "ue": "UE",
        "ui": "UI",
        "uo": "UO",
        "ja": "IA",
        "je": "IE",
        "jo": "OO",
        "ju": "IU",
        "wa": "UA",
        "we": "UE",
        "wi": "UI",
        "wo": "UO",
    }
)

IPA_MAP_ES_SINGLE: Final[MappingProxyType[IpaStr, ArpaStr]] = MappingProxyType(
    {
        "a": "A",
        "e": "E",
        "i": "I",
        "o": "O",
        "u": "U",
        "b": "B",
        "β": "BV",
        "ʧ": "CH",
        "ʃ": "CH",
        "d": "D",
        "ð": "DH",
        "f": "F",
        "ɡ": "G",
        "g": "G",
        "ɣ": "GH",
        "ʝ": "J",
        "j": "Y",
        "k": "K",
        "l": "L",
        "m": "M",
        "ɱ": "MM",
        "n": "N",
        "ɲ": "NN",
        "ŋ": "NG",
        "p": "P",
        "ɾ": "DX",
        "r": "RR",
        "s": "S",
        "t": "T",
        "w": "W",
        "x": "X",
        "̩": "",
        "ː": "",
        "͡": "",
        "/": "SYL",
    }
)

IPA_MAP_INCORRECT_SINGLE_ES_MX: Final[MappingProxyType[IpaStr, IpaStr]] = MappingProxyType(
    {
        "h": "x",
        "θ": "s",
        "v": "f",
        "ʎ": "ʝ",
        "z": "s",
    }
)

IPA_MAP_INCORRECT_DOUBLE_ES_MX: Final[MappingProxyType[IpaStr, IpaStr]] = MappingProxyType(
    {
        "tʃ": "ʧ",
        "ɟʝ": "ʝ",
    }
)

ARPA_TO_IPA_OVERRIDES_ES_MX: Final[MappingProxyType[ArpaStr, IpaStr]] = MappingProxyType(
    {
        "G": "g",
        "UI": "wi",
        "UA": "ua",
        "UE": "ue",
        "UO": "uo",
        "IE": "je",
        "IA": "ja",
        "IU": "ju",
    }
)

IPA_LIST_CONFLICTING_SYMBOLS_ES_MX: Final[tuple[IpaStr, ...]] = ()

ARPA_TO_AMIRABET_ES_MX_ONLY: Final[MappingProxyType[ArpaStr, AmirabetStr]] = MappingProxyType(
    {
        "GH": "ɣ",
        "DX": "ɾ",
        "IU": "ü",
        "AU": "ä",
        "RR": "r",
        "BV": "β",
        "OI": "ó",
        "O": "ɔ",
        "AI": "á",
        "IE": "é",
        "A": "ɑ",
        "X": "x",
        "I": "i",
        "UA": "ú",
        "U": "u",
        "NN": "ɲ",
        "E": "e",
        "UO": "y",
        "J": "ʝ",
        "IO": "í",
        "UE": "ū",
        "CH": "ʧ",
        "OO": "ö",
        "EU": "ē",
        "IA": "ā",
        "UI": "ī",
        "Y": "j",
        "MM": "ṁ",
        "EI": "ĕ",
    }
)

IPA_EQUIV: Final[MappingProxyType[int, IpaStr]] = MappingProxyType(
    {
        609: "g",
        601: "ʌ",
        ord("ɚ"): "ɝ",
        ord("r"): "ɹ",
    }
)
