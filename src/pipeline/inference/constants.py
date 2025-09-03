import importlib.resources as ilr
import json
from enum import StrEnum
from pathlib import Path
from typing import Final

BYTES_PER_GB: Final[int] = 1024**3
MS_PER_SECOND: Final[int] = 1000
try:
    _resource = ilr.files("src.pipeline").joinpath("valid_phonetic_elements.json")
    _phonetic_elements_path: Path = Path(str(_resource))
    _valid_phonetics_raw = _resource.read_text()
except Exception:
    _phonetic_elements_path = Path(__file__).resolve().parents[1] / "valid_phonetic_elements.json"
    _valid_phonetics_raw = _phonetic_elements_path.read_text()

PHONETIC_ELEMENTS_PATH: Final[Path] = _phonetic_elements_path


class DeviceType(StrEnum):
    CPU = "cpu"
    GPU = "cuda"
    MPS = "mps"


class TokenType(StrEnum):
    PAD = "<pad>"
    SEPARATOR = "|"


VALID_PHONETIC_ELEMENTS: Final[list[str]] = sorted(
    json.loads(_valid_phonetics_raw),
    key=len,
    reverse=True,
)
