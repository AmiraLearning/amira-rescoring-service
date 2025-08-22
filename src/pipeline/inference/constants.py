from enum import StrEnum
from pathlib import Path
from typing import Final
import json


BYTES_PER_GB: Final[int] = 1024**3
MS_PER_SECOND: Final[int] = 1000
PHONETIC_ELEMENTS_PATH: Final[Path] = Path("src/pipeline/valid_phonetic_elements.json")


class DeviceType(StrEnum):
    CPU = "cpu"
    GPU = "cuda"


class TokenType(StrEnum):
    PAD = "<pad>"
    SEPARATOR = "|"


VALID_PHONETIC_ELEMENTS: Final[list[str]] = sorted(
    json.loads(PHONETIC_ELEMENTS_PATH.read_text()),
    key=len,
    reverse=True,
)
