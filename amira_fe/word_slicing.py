from typing import Final

import pandas as pd


TimingSlice = tuple[float, float]

SKIPPED_UTTERANCE_CONFIDENCE: Final[float] = 0.05
SKIPPED_UTTERANCE_CONFIDENCE2: Final[float] = 0.95
SKIPPED_UTTERANCE_START_TIME: Final[float] = 1e-3
SKIPPED_UTTERANCE_END_TIME: Final[float] = 1e-3
SKIPPED_UTTERANCE_LAPSE: Final[int] = 0
SKIPPED_UTTERANCE_FRAME: Final[int] = 0

REQUIRED_SLICING_COLS: Final[list[str]] = [
    "kaldi_confidence",
    "kaldiNa_confidence",
    "Kaldi_Start_Time",
    "Kaldi_End_Time",
    "KaldiNa_Start_Time",
    "KaldiNa_End_Time",
    "Expected",
    "Kaldi_Rec_Word",
    "KaldiNa_Rec_Word",
]

_CONFIDENCE_THRESHOLD: Final[float] = 0.05001
_CONFIDENCE_TOLERANCE: Final[float] = 0.000015
_MIN_DURATION_THRESHOLD: Final[float] = 0.0011
_OVERLAP_THRESHOLD: Final[float] = 0.8


def _is_valid_time_slice(*, confidence: float, start_time: float, end_time: float) -> bool:
    """Validate ASR timing slice based on confidence and duration thresholds.

    Args:
        confidence: Confidence of ASR timing slice
        start_time: Start time of ASR timing slice
        end_time: End time of ASR timing slice

    Returns:
        True if ASR timing slice is valid, False otherwise
    """
    if pd.isnull(start_time) or pd.isnull(end_time):
        return False

    confidence_valid = abs(confidence - _CONFIDENCE_THRESHOLD) >= _CONFIDENCE_TOLERANCE
    duration_valid = end_time - start_time >= _MIN_DURATION_THRESHOLD

    return confidence_valid or duration_valid


def _calculate_overlap_ratio(
    *, kaldi_start: float, kaldi_end: float, kaldina_start: float, kaldina_end: float
) -> float:
    """Calculate overlap ratio between two timing intervals.

    Args:
        kaldi_start: Start time of Kaldi timing interval
        kaldi_end: End time of Kaldi timing interval
        kaldina_start: Start time of KaldiNa timing interval
        kaldina_end: End time of KaldiNa timing interval

    Returns:
        Overlap ratio between two timing intervals
    """
    union_duration = max(kaldi_end, kaldina_end) - min(kaldi_start, kaldina_start)
    intersection_duration = min(kaldi_end, kaldina_end) - max(kaldi_start, kaldina_start)

    if intersection_duration <= 0:
        return 0.0

    return intersection_duration / union_duration


def _should_prefer_kaldi_over_kaldina(
    *, kaldi_start: float, kaldi_end: float, kaldina_start: float, kaldina_end: float
) -> bool:
    """Determine if Kaldi timing should be preferred over KaldiNa when both are valid.

    Args:
        kaldi_start: Start time of Kaldi timing interval
        kaldi_end: End time of Kaldi timing interval
        kaldina_start: Start time of KaldiNa timing interval
        kaldina_end: End time of KaldiNa timing interval

    Returns:
        True if Kaldi timing should be preferred over KaldiNa, False otherwise
    """
    overlap_ratio = _calculate_overlap_ratio(
        kaldi_start=kaldi_start,
        kaldi_end=kaldi_end,
        kaldina_start=kaldina_start,
        kaldina_end=kaldina_end,
    )
    return overlap_ratio > _OVERLAP_THRESHOLD


def preferred_word_slice(
    *, row: pd.Series | dict[str, any], ignore_asrs: list[str]
) -> TimingSlice | None:
    """
    Select optimal word timing slice from available ASR systems.

    Prioritizes KaldiNa when it matches expected text and Kaldi doesn't,
    unless Kaldi and KaldiNa have significant overlap (>80%), in which
    case Kaldi is preferred for better segmentation.

    Args:
        row: DataFrame row or dictionary containing ASR timing data
        ignore_asrs: List of ASR system names to exclude from consideration

    Returns:
        Tuple of (start_time, end_time) or None if no valid timing available

    Raises:
        KeyError: If required columns are missing from row data
    """

    normalized_ignore_asrs = [asr.lower() for asr in ignore_asrs]

    kaldi_excluded = "kaldi" in normalized_ignore_asrs
    kaldina_excluded = "kaldina" in normalized_ignore_asrs

    kaldi_valid = not kaldi_excluded and _is_valid_time_slice(
        confidence=row["kaldi_confidence"],
        start_time=row["Kaldi_Start_Time"],
        end_time=row["Kaldi_End_Time"],
    )

    kaldina_valid = not kaldina_excluded and _is_valid_time_slice(
        confidence=row["kaldiNa_confidence"],
        start_time=row["KaldiNa_Start_Time"],
        end_time=row["KaldiNa_End_Time"],
    )

    expected_text = row["Expected"].lower()
    kaldi_matches = expected_text == row["Kaldi_Rec_Word"].lower()
    kaldina_matches = expected_text == row["KaldiNa_Rec_Word"].lower()

    if not kaldi_matches and kaldina_matches and kaldina_valid:
        if kaldi_valid and _should_prefer_kaldi_over_kaldina(
            kaldi_start=row["Kaldi_Start_Time"],
            kaldi_end=row["Kaldi_End_Time"],
            kaldina_start=row["KaldiNa_Start_Time"],
            kaldina_end=row["KaldiNa_End_Time"],
        ):
            return (row["Kaldi_Start_Time"], row["Kaldi_End_Time"])

        return (row["KaldiNa_Start_Time"], row["KaldiNa_End_Time"])

    if kaldi_valid:
        return (row["Kaldi_Start_Time"], row["Kaldi_End_Time"])

    return None
