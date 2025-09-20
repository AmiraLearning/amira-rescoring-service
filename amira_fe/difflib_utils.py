"""Utilities for processing difflib output with lower-level access.

Based on Python 3.7.10 difflib source, updated to provide lower level access.
Originally created June 2021.
"""

from collections.abc import Iterator
from enum import StrEnum
from types import MappingProxyType
from typing import Final, TypeAlias


class _DiffMarkers(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    SEPARATOR = "separator"
    TAB = "tab"


class _DiffMarkersSymbols(StrEnum):
    ADDED = "\0+"
    REMOVED = "\0-"
    CHANGED = "\0^"
    SEPARATOR = "\1"
    TAB = "\t"


_DIFF_MARKERS_MAPPING: Final[MappingProxyType[_DiffMarkers, _DiffMarkersSymbols]] = (
    MappingProxyType(
        {
            _DiffMarkers.ADDED: _DiffMarkersSymbols.ADDED,
            _DiffMarkers.REMOVED: _DiffMarkersSymbols.REMOVED,
            _DiffMarkers.CHANGED: _DiffMarkersSymbols.CHANGED,
            _DiffMarkers.SEPARATOR: _DiffMarkersSymbols.SEPARATOR,
            _DiffMarkers.TAB: _DiffMarkersSymbols.TAB,
        }
    )
)

_PLACEHOLDER_LINE: Final[str] = "-"

_DiffLine: TypeAlias = tuple[int | str, str]
_DiffEntry: TypeAlias = tuple[_DiffLine, _DiffLine, str]


def collect_lines(
    *, diffs: Iterator[_DiffEntry]
) -> tuple[
    list[str | None],
    list[str | None],
    list[int | str | None],
    list[int | str | None],
]:
    """Collect mdiff output into separate lists for processing.

    Processes mdiff-style iterator output and separates the from/to data
    into distinct lists. Handles context separators by inserting None values
    where exceptions occur during line processing.

    Args:
        diffs: Iterator yielding tuples of (fromdata, todata, flags) where
               fromdata and todata contain line information for formatting

    Returns:
        Tuple containing (fromlist, tolist, fromindex, toindex) where each
        list contains the processed text or line numbers, with None values
        for context separator positions
    """
    fromlist: list[str | None] = []
    tolist: list[str | None] = []
    fromindex: list[int | str | None] = []
    toindex: list[int | str | None] = []

    for fromdata, todata, _ in diffs:
        try:
            linenum, text = format_line(linenum=fromdata[0], text=fromdata[1])
            fromlist.append(text)
            fromindex.append(linenum)

            linenum, text = format_line(linenum=todata[0], text=todata[1])
            tolist.append(text)
            toindex.append(linenum)
        except TypeError:
            fromlist.append(None)
            fromindex.append(None)
            tolist.append(None)
            toindex.append(None)

    return fromlist, tolist, fromindex, toindex


def format_line(*, linenum: int | str, text: str) -> tuple[str, str]:
    """Format and clean diff line text by removing annotation markers.

    Processes line text to remove difflib annotation markers that indicate
    additions, removals, changes, and separators. Also handles line number
    formatting for display purposes. Exceptions occur for blank lines where
    linenum is empty string, which are handled by returning placeholder values.

    Args:
        linenum: Line number for display (used for line number column)
        text: Raw line text containing difflib annotation markers

    Returns:
        Tuple of (formatted_linenum, cleaned_text) where markers are removed
        and text is right-stripped of whitespace
    """
    try:
        formatted_linenum = f"{linenum:d}"
        cleaned_text = text
        for marker in _DIFF_MARKERS_MAPPING.values():
            cleaned_text = cleaned_text.replace(marker, "")
        cleaned_text = cleaned_text.rstrip()
    except (TypeError, ValueError):
        formatted_linenum = _PLACEHOLDER_LINE
        cleaned_text = _PLACEHOLDER_LINE

    return formatted_linenum, cleaned_text
