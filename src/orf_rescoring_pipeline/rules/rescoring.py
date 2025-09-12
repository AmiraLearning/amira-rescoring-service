"""
Phrase rescoring functionality for ORF rescoring pipeline.

This module contains functions for transcribing phrases, aligning transcripts,
and rescoring phrase errors based on multiple ASR system outputs.
"""

import asyncio
import logging
import os

import numpy as np

from orf_rescoring_pipeline.alignment.word_alignment import (
    get_word_level_transcript_alignment,
    get_word_level_transcript_alignment_w2v,
)
from orf_rescoring_pipeline.models import Activity, ModelFeature
from orf_rescoring_pipeline.utils.transcription import KaldiASRClient, W2VASRClient

logger = logging.getLogger(__name__)


def transcribe_phrase(
    *,
    activity: Activity,
    phrase_index: int,
    kaldi: KaldiASRClient,
    w2v: W2VASRClient,
) -> tuple[str, str]:
    """
    Transcribe a phrase using Kaldi and W2V ASR services in parallel.

    Args:
        activity: Activity containing the audio data.
        phrase_index: Index of the phrase to transcribe.
        kaldi: Kaldi ASR client.
        w2v: W2V ASR client.

    Returns:
        Tuple of (kaldi_transcript, w2v_transcript).
    """
    from concurrent.futures import ThreadPoolExecutor

    from orf_rescoring_pipeline.alignment.audio_processing import slice_audio_file_data

    phrase_audio_data, (start_ms, end_ms) = slice_audio_file_data(
        activity=activity, phrase_index=phrase_index
    )
    if phrase_audio_data is None or (
        start_ms is not None and end_ms is not None and end_ms - start_ms == 0
    ):
        logger.warning(f"No phrase audio data for {activity.activity_id}, phrase:{phrase_index}")
        return "", ""

    kaldi_transcript = ""
    w2v_transcript = ""

    max_workers_env = os.getenv("PIPELINE_INNER_CONCURRENCY", "2")
    try:
        max_workers = max(1, int(max_workers_env))
    except Exception:
        max_workers = 2

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        loop = asyncio.new_event_loop()
        try:
            kaldi_future = executor.submit(
                lambda: loop.run_until_complete(
                    kaldi.transcribe(activity, phrase_index, phrase_audio_data)
                )
            )
            w2v_future = executor.submit(w2v.transcribe, activity, phrase_index, phrase_audio_data)

            try:
                kaldi_result = kaldi_future.result()
                kaldi_transcript = kaldi_result.transcript if kaldi_result else ""
            except Exception as e:
                logger.warning(f"Transcription failed for kaldi: {e}")

            try:
                w2v_result = w2v_future.result()
                w2v_transcript = w2v_result.transcript if w2v_result else ""
            except Exception as e:
                logger.warning(f"Transcription failed for w2v: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    return kaldi_transcript, w2v_transcript


def align_phrase_transcripts(
    *,
    story_phrase: str,
    kaldi_transcript: str,
    w2v_transcript: str,
) -> tuple[list[int], list[int]]:
    """
    Align transcripts with the expected story phrase to get word-level matches.

    Args:
        story_phrase: The expected phrase from the story.
        kaldi_transcript: Transcript from Kaldi ASR.
        w2v_transcript: Transcript from W2V ASR.

    Returns:
        Tuple of (kaldi_matches, w2v_matches) where each is a list of 0/1 integers.
    """
    kaldi_matches = get_word_level_transcript_alignment(
        story_phrase=story_phrase, transcript_text=kaldi_transcript
    )
    w2v_matches = get_word_level_transcript_alignment_w2v(
        story_phrase=story_phrase, transcript_text=w2v_transcript
    )

    return kaldi_matches, w2v_matches


def rescore_phrase_errors(
    *,
    deepgram_matches: list[int],
    kaldi_matches: list[int],
    w2v_matches: list[int],
) -> list[bool]:
    """
    Rescore phrase errors based on alignment matches from different ASR systems.

    Uses vectorized implementation with NumPy for better performance.

    Args:
        deepgram_matches: List of 0/1 matches from Deepgram ASR.
        kaldi_matches: List of 0/1 matches from Kaldi ASR.
        w2v_matches: List of 0/1 matches from W2V ASR.

    Returns:
        List of True/False indicating whether each word is an error.
    """
    deepgram = np.array(deepgram_matches)
    kaldi = np.array(kaldi_matches)
    w2v = np.array(w2v_matches)

    no_error_w2v = w2v == 1

    total_matches = deepgram + kaldi + w2v
    no_error_consensus = total_matches > 1

    is_error = ~(no_error_w2v | no_error_consensus)

    return is_error.tolist()


def rescore_phrase(
    *,
    activity: Activity,
    feature: ModelFeature,
    kaldi: KaldiASRClient,
    w2v: W2VASRClient,
) -> tuple[list[bool], list[int], list[int], str, str]:
    """
    Rescore a phrase using transcription, alignment, and error rescoring functions.

    Args:
        activity: Activity containing the audio data.
        feature: ModelFeature containing phrase information and existing matches.
        kaldi: Kaldi ASR client.
        w2v: W2V ASR client.

    Returns:
        Tuple of (error_array, kaldi_matches, w2v_matches, kaldi_transcript, w2v_transcript).
    """
    from orf_rescoring_pipeline.alignment.audio_processing import slice_audio_file_data

    phrase_audio_data, (start_ms, end_ms) = slice_audio_file_data(
        activity=activity, phrase_index=feature.phrase_index
    )
    if phrase_audio_data is None or (
        start_ms is not None and end_ms is not None and end_ms - start_ms == 0
    ):
        logger.warning(
            f"No phrase audio data for {activity.activity_id}, phrase:{feature.phrase_index}"
        )
        fallback_matches = [0] * len(feature.kaldi_match)
        fallback_errors = [True] * len(feature.kaldi_match)
        return fallback_errors, fallback_matches, fallback_matches, "", ""

    kaldi_transcript, w2v_transcript = transcribe_phrase(
        activity=activity, phrase_index=feature.phrase_index, kaldi=kaldi, w2v=w2v
    )

    kaldi_matches, w2v_matches = align_phrase_transcripts(
        story_phrase=activity.phrases[feature.phrase_index],
        kaldi_transcript=kaldi_transcript,
        w2v_transcript=w2v_transcript,
    )

    error_array = rescore_phrase_errors(
        deepgram_matches=feature.deepgram_match,
        kaldi_matches=kaldi_matches,
        w2v_matches=w2v_matches,
    )

    return error_array, kaldi_matches, w2v_matches, kaldi_transcript, w2v_transcript
