"""
Audio extraction functionality for the offline pipeline.

This module handles both page-level and phrase-level audio extraction:
- Page-level: Extract audio segments for complete pages
- Phrase-level: Extract audio segments for individual aligned phrases

The extracted audio files are organized in separate directories for easy management.
"""

import logging
from pathlib import Path
from typing import Any

from pydub import AudioSegment

from src.orf_rescoring_pipeline.models import Activity

logger = logging.getLogger(__name__)


def extract_page_audio(activity: Activity, manifest_pages: list[Any]) -> list[str]:
    """
    Extract page-level audio files and return file paths.

    Creates audio segments for each page based on manifest timing data.
    Each page gets its own audio file in the page_audio/ directory.

    Args:
        activity: Activity object containing audio data
        manifest_pages: List of page data from phrase manifest

    Returns:
        List of paths to extracted page audio files

    Raises:
        Exception: If audio extraction fails
    """
    logger.info(
        f"Activity {activity.activity_id}: Extracting page-level audio for {len(manifest_pages)} pages"
    )

    pipeline_dir = Path(__file__).parent.parent
    activity_dir = pipeline_dir / "temp" / activity.activity_id
    wav_path = activity_dir / f"{activity.activity_id}_complete.wav"

    if not wav_path.exists():
        logger.error(f"Activity {activity.activity_id}: Missing audio file: {wav_path}")
        return []

    try:
        audio = AudioSegment.from_wav(wav_path)
        page_audio_dir = activity_dir / "page_audio"
        page_audio_dir.mkdir(exist_ok=True)

        page_files: list[str] = []
        for page_data in manifest_pages:
            page_index = page_data.phrase
            start_ms = page_data.start
            end_ms = page_data.end

            if end_ms > start_ms:
                logger.debug(
                    f"Activity {activity.activity_id}: Extracting page {page_index} audio ({start_ms}ms - {end_ms}ms)"
                )
                page_segment = audio[start_ms:end_ms]
                page_path = page_audio_dir / f"page_{page_index}.wav"
                page_segment.export(page_path, format="wav")
                page_files.append(str(page_path))
                logger.debug(
                    f"Activity {activity.activity_id}: Page {page_index} audio saved to {page_path}"
                )
            else:
                logger.warning(
                    f"Activity {activity.activity_id}: Page {page_index} has invalid timing ({start_ms}ms - {end_ms}ms)"
                )

        logger.info(
            f"Activity {activity.activity_id}: Extracted {len(page_files)} page audio files"
        )
        return page_files

    except Exception as e:
        logger.error(f"Activity {activity.activity_id}: Error extracting page audio: {e}")
        return []


def extract_phrase_audio(activity: Activity) -> list[str]:
    """
    Extract phrase-level audio files from page-level aligned data and return file paths.

    Creates audio segments for each aligned phrase using the timing data from
    the page-level alignment process. Files are named with absolute phrase indices.

    Args:
        activity: Activity object with aligned phrase data

    Returns:
        List of paths to extracted phrase audio files

    Raises:
        Exception: If audio extraction fails
    """
    logger.info(
        f"Activity {activity.activity_id}: Extracting phrase-level audio for aligned phrases"
    )

    pipeline_dir = Path(__file__).parent.parent
    activity_dir = pipeline_dir / "temp" / activity.activity_id
    wav_path = activity_dir / f"{activity.activity_id}_complete.wav"

    if not wav_path.exists():
        logger.error(f"Activity {activity.activity_id}: Missing audio file: {wav_path}")
        return []

    try:
        audio = AudioSegment.from_wav(wav_path)
        phrase_audio_dir = activity_dir / "phrase_audio"
        phrase_audio_dir.mkdir(exist_ok=True)

        phrase_files = []
        total_phrases = 0

        for page in activity.pages:
            if not page.aligned_phrases:
                continue

            for phrase_boundary in page.aligned_phrases:
                relative_phrase_idx = phrase_boundary["phrase_idx"]
                absolute_phrase_idx = page.phrase_indices[relative_phrase_idx]
                start_ms = phrase_boundary["start"]
                end_ms = phrase_boundary["end"]

                if start_ms is not None and end_ms is not None and end_ms > start_ms:
                    logger.debug(
                        f"Activity {activity.activity_id}: Extracting phrase {absolute_phrase_idx} audio ({start_ms}ms - {end_ms}ms)"
                    )
                    phrase_segment = audio[start_ms:end_ms]
                    phrase_path = (
                        phrase_audio_dir
                        / f"phrase_{absolute_phrase_idx:03d}_page_{page.page_index}.wav"
                    )
                    phrase_segment.export(phrase_path, format="wav")
                    phrase_files.append(str(phrase_path))
                    total_phrases += 1
                    logger.debug(
                        f"Activity {activity.activity_id}: Phrase {absolute_phrase_idx} audio saved to {phrase_path}"
                    )
                else:
                    logger.warning(
                        f"Activity {activity.activity_id}: Phrase {absolute_phrase_idx} has invalid timing ({start_ms}ms - {end_ms}ms)"
                    )

        logger.info(
            f"Activity {activity.activity_id}: Extracted {total_phrases} phrase audio files from {len([p for p in activity.pages if p.aligned_phrases])} pages"
        )
        return phrase_files

    except Exception as e:
        logger.error(f"Activity {activity.activity_id}: Error extracting phrase audio: {e}")
        return []
