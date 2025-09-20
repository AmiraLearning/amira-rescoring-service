"""Alignment functionality for the offline pipeline.

This module contains the core alignment logic that matches expected phrases
to transcribed speech at the page level. The alignment process:

1. Filters tokens to page boundaries
2. Uses sequence matching to align expected vs actual text
3. Generates phrase-level timing boundaries
4. Saves detailed timing information to JSON
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from amira_pyutils.logging import get_logger
from src.orf_rescoring_pipeline.models import Activity, WordItem

logger = get_logger(__name__)

# Constants
MAX_CONCURRENT_PAGES: Final[int] = 4
PAGE_TIME_BUFFER_MS: Final[int] = 1_000
TIMING_FILE_SUFFIX: Final[str] = "_timing_deepgram.json"
TRANSCRIPT_DIR_NAME: Final[str] = "deepgram_transcription_and_slicing_data"

# Retry configuration
RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_MIN_WAIT_SECONDS: Final[float] = 1.0
RETRY_MAX_WAIT_SECONDS: Final[float] = 10.0


def _filter_tokens_to_page_range(
    *,
    tokens: list[WordItem],
    page_start_ms: int,
    page_end_ms: int,
) -> list[WordItem]:
    """Filter tokens to only those within the page time range.

    Args:
        tokens: List of WordItem objects from transcription.
        page_start_ms: Start time of page in milliseconds.
        page_end_ms: End time of page in milliseconds.

    Returns:
        Filtered list of tokens within the page boundaries.
    """
    return [
        token
        for token in tokens
        if page_start_ms <= token.start * 1000 <= page_end_ms + PAGE_TIME_BUFFER_MS
    ]


def _prepare_phrases_for_alignment(*, expected_phrases: list[str]) -> tuple[list[str], list[int]]:
    """Prepare phrases for sequence alignment by flattening and tracking word counts.

    Args:
        expected_phrases: List of expected phrase strings.

    Returns:
        Tuple of (combined_words, word_counts_per_phrase).
    """
    combined_words: list[str] = []
    word_counts: list[int] = []

    for phrase in expected_phrases:
        words = phrase.split()
        word_counts.append(len(words))
        combined_words.extend(words)

    return combined_words, word_counts


def _build_word_mapping(
    *,
    token_words: list[str],
    expected_words: list[str],
) -> dict[int, int]:
    """Build mapping from expected word indices to token indices using sequence matching.

    Args:
        token_words: List of transcribed word strings.
        expected_words: List of expected word strings.

    Returns:
        Dictionary mapping expected word indices to token indices.
    """
    matcher = SequenceMatcher(None, token_words, expected_words)
    opcodes = matcher.get_opcodes()

    word_mapping: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ("equal", "replace"):
            length = min(i2 - i1, j2 - j1)
            for offset in range(length):
                word_mapping[j1 + offset] = i1 + offset

    return word_mapping


def _create_phrase_boundary(
    *,
    phrase_idx: int,
    word_mapping: dict[int, int],
    expected_word_start: int,
    expected_word_end: int,
    page_tokens: list[WordItem],
    token_words: list[str],
) -> dict[str, Any]:
    """Create a single phrase boundary with timing information.

    Args:
        phrase_idx: Index of the phrase.
        word_mapping: Mapping from expected to token indices.
        expected_word_start: Start index in expected words.
        expected_word_end: End index in expected words.
        page_tokens: List of tokens within the page.
        token_words: List of token word strings.

    Returns:
        Dictionary containing phrase boundary information.
    """
    mapped_indices = [
        word_mapping.get(idx) for idx in range(expected_word_start, expected_word_end)
    ]
    valid_indices = [idx for idx in mapped_indices if idx is not None]

    if valid_indices:
        start_token_idx = valid_indices[0]
        end_token_idx = valid_indices[-1]
        start_ms = int(page_tokens[start_token_idx].start * 1000)
        end_ms = int(page_tokens[end_token_idx].end * 1000)
        parsed_text = " ".join(token_words[start_token_idx : end_token_idx + 1])
    else:
        start_ms = None
        end_ms = None
        parsed_text = ""

    return {
        "phrase_idx": phrase_idx,
        "start": start_ms,
        "end": end_ms,
        "parsed": parsed_text,
    }


def _enforce_timing_continuity(
    *,
    boundaries: list[dict[str, Any]],
    page_start_ms: int,
) -> None:
    """Enforce timing continuity within page boundaries.

    Args:
        boundaries: List of phrase boundaries to modify in-place.
        page_start_ms: Start time of the page in milliseconds.

    Side Effects:
        Modifies boundaries list in-place to ensure timing continuity.
    """
    last_end_ms = page_start_ms

    for boundary in boundaries:
        if boundary["start"] is None or boundary["start"] < last_end_ms:
            boundary["start"] = last_end_ms
        if boundary["end"] is None or boundary["end"] < boundary["start"]:
            boundary["end"] = boundary["start"]
        last_end_ms = boundary["end"]


def align_page_phrases(
    *,
    expected_phrases: list[str],
    tokens: list[WordItem],
    page_start_ms: int | None,
    page_end_ms: int | None,
) -> list[dict[str, Any]]:
    """Align expected phrases for a specific page with transcribed tokens.

    This is the core alignment algorithm that matches expected phrases to
    actual transcribed words within a page's time boundaries.

    Args:
        expected_phrases: List of expected phrase strings for this page.
        tokens: List of WordItem objects from transcription.
        page_start_ms: Start time of page in milliseconds.
        page_end_ms: End time of page in milliseconds.

    Returns:
        List of phrase boundaries with absolute timings.

    Example:
        >>> phrases = ["hello world", "good morning"]
        >>> tokens = [WordItem("hello", 1.0, 1.5, 0.9), WordItem("world", 1.5, 2.0, 0.8)]
        >>> boundaries = align_page_phrases(
        ...     expected_phrases=phrases,
        ...     tokens=tokens,
        ...     page_start_ms=1000,
        ...     page_end_ms=3000
        ... )
        >>> boundaries[0]["start"]
        1000
    """
    if not tokens or not expected_phrases or page_start_ms is None or page_end_ms is None:
        return []

    page_tokens = _filter_tokens_to_page_range(
        tokens=tokens,
        page_start_ms=page_start_ms,
        page_end_ms=page_end_ms,
    )

    if not page_tokens:
        return []

    expected_words, word_counts = _prepare_phrases_for_alignment(expected_phrases=expected_phrases)

    token_words = [token.word for token in page_tokens]
    word_mapping = _build_word_mapping(
        token_words=token_words,
        expected_words=expected_words,
    )

    boundaries: list[dict[str, Any]] = []
    expected_word_pos = 0

    for phrase_idx, word_count in enumerate(word_counts):
        boundary = _create_phrase_boundary(
            phrase_idx=phrase_idx,
            word_mapping=word_mapping,
            expected_word_start=expected_word_pos,
            expected_word_end=expected_word_pos + word_count,
            page_tokens=page_tokens,
            token_words=token_words,
        )
        boundaries.append(boundary)
        expected_word_pos += word_count

    _enforce_timing_continuity(
        boundaries=boundaries,
        page_start_ms=page_start_ms,
    )

    return boundaries


def _create_phrase_data_with_absolute_indices(
    *,
    phrase_boundary: dict[str, Any],
    page_phrase_indices: list[int],
    page_phrases: list[str],
) -> dict[str, Any]:
    """Create phrase data with absolute indices and text.

    Args:
        phrase_boundary: Boundary data for the phrase.
        page_phrase_indices: List of absolute phrase indices for the page.
        page_phrases: List of phrase texts for the page.

    Returns:
        Dictionary containing phrase data with absolute indices.
    """
    relative_phrase_idx = phrase_boundary["phrase_idx"]
    absolute_phrase_idx = page_phrase_indices[relative_phrase_idx]
    phrase_text = (
        page_phrases[relative_phrase_idx] if relative_phrase_idx < len(page_phrases) else ""
    )

    return {
        "phrase_idx": absolute_phrase_idx,
        "start": phrase_boundary["start"],
        "end": phrase_boundary["end"],
        "parsed": phrase_boundary.get("parsed", ""),
        "phrase_text": phrase_text,
    }


def _create_default_phrase_data(
    *,
    page_phrase_indices: list[int],
    page_phrases: list[str],
    page_start_time: int,
) -> list[dict[str, Any]]:
    """Create default phrase data for pages without aligned phrases.

    Args:
        page_phrase_indices: List of absolute phrase indices for the page.
        page_phrases: List of phrase texts for the page.
        page_start_time: Start time of the page.

    Returns:
        List of default phrase data dictionaries.
    """
    return [
        {
            "phrase_idx": page_phrase_indices[phrase_idx],
            "start": page_start_time,
            "end": page_start_time,
            "parsed": "",
            "phrase_text": phrase,
        }
        for phrase_idx, phrase in enumerate(page_phrases)
    ]


@retry(
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(
        multiplier=RETRY_MIN_WAIT_SECONDS,
        max=RETRY_MAX_WAIT_SECONDS,
    ),
    retry=retry_if_exception_type((OSError, IOError)),
)
def save_timing_file(*, activity: Activity, timing_path: Path) -> None:
    """Save timing data to JSON file with retry logic.

    Args:
        activity: Activity object containing page and timing data.
        timing_path: Path where timing file should be saved.

    Raises:
        OSError: If file operations fail after retries.
        IOError: If file I/O operations fail after retries.
    """
    page_timing_data: list[dict[str, Any]] = []

    for page in activity.pages:
        if page.aligned_phrases:
            phrases_with_absolute_indices = [
                _create_phrase_data_with_absolute_indices(
                    phrase_boundary=phrase_boundary,
                    page_phrase_indices=page.phrase_indices,
                    page_phrases=page.phrases,
                )
                for phrase_boundary in page.aligned_phrases
            ]
        else:
            phrases_with_absolute_indices = _create_default_phrase_data(
                page_phrase_indices=page.phrase_indices,
                page_phrases=page.phrases,
                page_start_time=page.start_time or 0,
            )

        page_timing_data.append(
            {
                "page_index": page.page_index,
                "page_start": page.start_time,
                "page_end": page.end_time,
                "phrases": phrases_with_absolute_indices,
            }
        )

    output_data = {
        "full_transcript": activity.transcript_json.transcript if activity.transcript_json else "",
        "pages": page_timing_data,
    }

    timing_path.parent.mkdir(parents=True, exist_ok=True)
    with open(timing_path, "w") as file:
        json.dump(output_data, file, indent=2)

    activity.timing_path = str(timing_path)


def _process_single_page(
    *,
    page_index: int,
    activity: Activity,
    manifest_pages: list[Any],
) -> tuple[int, int, int]:
    """Process alignment for a single page.

    Args:
        page_index: Index of the page to process.
        activity: Activity object containing page data.
        manifest_pages: List of manifest page data.

    Returns:
        Tuple of (page_index, aligned_count, total_phrases).

    Side Effects:
        Updates the activity page with timing and alignment data.
    """
    page_data = activity.pages[page_index]
    manifest_page = manifest_pages[page_index]

    page_data.start_time = manifest_page.start
    page_data.end_time = manifest_page.end

    logger.debug(
        f"Activity {activity.activity_id}, Page {page_index}: "
        f"Time range {page_data.start_time}ms - {page_data.end_time}ms"
    )

    page_boundaries = align_page_phrases(
        expected_phrases=page_data.phrases,
        tokens=activity.transcript_json.words if activity.transcript_json else [],
        page_start_ms=page_data.start_time,
        page_end_ms=page_data.end_time,
    )

    page_data.aligned_phrases = page_boundaries

    logger.debug(
        f"Activity {activity.activity_id}, Page {page_index}: "
        f"Aligned {len(page_boundaries)} out of {len(page_data.phrases)} phrases"
    )

    return page_index, len(page_boundaries), len(page_data.phrases)


def _get_timing_file_path(*, activity: Activity) -> Path:
    """Get the path for the timing file.

    Args:
        activity: Activity object to generate path for.

    Returns:
        Path object for the timing file.
    """
    pipeline_dir = Path(__file__).parent.parent
    activity_dir = pipeline_dir / TRANSCRIPT_DIR_NAME
    return activity_dir / f"{activity.activity_id}{TIMING_FILE_SUFFIX}"


def process_activity_timing(
    *,
    activity: Activity,
    manifest_pages: list[Any],
    save_files: bool = True,
) -> None:
    """Process page-level timing alignment for an activity.

    This function coordinates the entire alignment process:
    1. Validates that transcript and page data are available
    2. Processes each page with available audio data
    3. Aligns phrases within each page's time boundaries
    4. Saves detailed timing information to JSON file

    Args:
        activity: Activity object to process.
        manifest_pages: List of page data from phrase manifest.
        save_files: Whether to save timing files to disk.

    Side Effects:
        Updates activity.pages with aligned phrase data.
        Sets activity.timing_path to saved JSON file if save_files is True.

    Raises:
        ValueError: If activity lacks required transcript or page data.
        RuntimeError: If page processing fails.
    """
    logger.debug(f"Starting page-level timing alignment for activity {activity.activity_id}")

    if not activity.transcript_json:
        raise ValueError(f"Activity {activity.activity_id}: No transcript available")

    if not activity.pages:
        raise ValueError(f"Activity {activity.activity_id}: No page data available")

    max_audio_pages = len(manifest_pages)
    available_pages = min(len(activity.pages), max_audio_pages)

    logger.debug(
        f"Activity {activity.activity_id}: Processing {available_pages} pages "
        f"(story has {len(activity.pages)}, audio has {max_audio_pages})"
    )

    max_workers = min(available_pages, MAX_CONCURRENT_PAGES)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_single_page,
                page_index=i,
                activity=activity,
                manifest_pages=manifest_pages,
            ): i
            for i in range(available_pages)
        }

        for future in as_completed(futures):
            try:
                page_index, aligned_count, total_phrases = future.result()
                logger.debug(
                    f"Activity {activity.activity_id}, Page {page_index}: Processing completed"
                )
            except Exception as e:
                logger.error(f"Activity {activity.activity_id}: Page processing failed: {e}")
                raise RuntimeError(f"Page processing failed: {e}") from e

    skipped_pages = len(activity.pages) - available_pages
    if skipped_pages > 0:
        logger.warning(
            f"Activity {activity.activity_id}: Skipped {skipped_pages} pages "
            f"due to missing audio data"
        )
        for i in range(available_pages, len(activity.pages)):
            activity.pages[i].aligned_phrases = []

    if save_files:
        timing_path = _get_timing_file_path(activity=activity)
        save_timing_file(activity=activity, timing_path=timing_path)

    total_aligned = sum(
        len(page.aligned_phrases) for page in activity.pages if page.aligned_phrases
    )
    logger.debug(
        f"Activity {activity.activity_id}: Successfully aligned {total_aligned} phrases "
        f"across {len(activity.pages)} pages"
    )
