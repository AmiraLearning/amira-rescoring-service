import threading
from pathlib import Path
from typing import Final

import amira_pyutils.s3 as s3_utils
from amira_fe.phon_level_alignment import phoneme_align_dash
from amira_fe.word_level_alignment import align_texts_dash
from amira_pyutils.abstract_alignment import AlignmentConfig, WordSelectionStrategy
from amira_pyutils.language import LanguageHandling
from amira_pyutils.logging import get_logger
from amira_pyutils.phone_alphabets import english2amirabet

# TODO this is the meet of the imports

logger = get_logger(__name__)

S3_PHONEME_DICT_URI: Final[str] = "s3://amira-kaldi-lm-repo/alts/all_story_words.dic"
PHONEME_DICT_FILENAME: Final[str] = "all_story_words.dic"
ALIGNMENT_GAP_MARKER: Final[str] = "-"
WORD_MATCH_SCORE: Final[int] = 1
WORD_NO_MATCH_SCORE: Final[int] = 0
EXACT_PHONEME_DISTANCE: Final[int] = 0

_download_lock = threading.Lock()
_phoneme_dict_cache: dict[str, str] | None = None


# TODO(amira_pyutils/s3_client): Replace sync bridge with real client wiring
def _load_phoneme_dict_from_s3_sync() -> dict[str, str]:
    try:
        import asyncio

        return asyncio.run(_load_phoneme_dict_from_s3())
    except Exception:
        return {}


def _get_phoneme_dict_path() -> Path:
    """Get the local path for the phoneme dictionary file.

    Returns:
        Path to the phoneme dictionary file.
    """
    return Path(__file__).parent.parent / PHONEME_DICT_FILENAME


async def _download_phoneme_dict_from_s3(*, file_path: Path) -> None:
    """Download phoneme dictionary from S3 to local cache.

    Args:
        file_path: Local path where dictionary should be saved.

    Raises:
        FileNotFoundError: If download fails.
    """
    logger.info("Downloading phoneme dictionary from S3...")
    address = s3_utils.S3Address.from_uri(uri=S3_PHONEME_DICT_URI)
    await s3_utils.S3Service().download_file(
        bucket=address.bucket, key=address.key, filename=file_path
    )
    if not file_path.exists():
        raise FileNotFoundError(
            f"Failed to download {address.bucket}/{address.key} to {file_path}."
        )
    logger.info(f"Phoneme dictionary cached at {file_path}")


async def _parse_phoneme_dict_file(*, file_path: Path) -> dict[str, str]:
    """Parse phoneme dictionary file into word-to-phoneme mapping.

    Args:
        file_path: Path to the phoneme dictionary file.

    Returns:
        Dictionary mapping words to their phoneme representations.
    """
    phoneme_dict: dict[str, str] = {}
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split()
                if len(parts) > 1:
                    phoneme_dict[parts[0]] = " ".join(parts[1:])
    return phoneme_dict


async def _load_phoneme_dict_from_s3() -> dict[str, str]:
    """Load the phoneme dictionary from S3 with thread-safe caching.

    Returns:
        Dictionary mapping words to their phoneme representations.

    Raises:
        FileNotFoundError: If dictionary cannot be downloaded from S3.
    """
    global _phoneme_dict_cache

    if _phoneme_dict_cache is not None:
        return _phoneme_dict_cache

    file_path = _get_phoneme_dict_path()

    if not file_path.exists():
        with _download_lock:
            if not file_path.exists():
                await _download_phoneme_dict_from_s3(file_path=file_path)

    _phoneme_dict_cache = await _parse_phoneme_dict_file(file_path=file_path)
    return _phoneme_dict_cache


def _create_standard_alignment_config() -> AlignmentConfig:
    """Create standard alignment configuration for word-level alignment.

    Returns:
        Configured AlignmentConfig instance.
    """
    return AlignmentConfig(
        use_production_algorithm=False, preprocess_next_expected=False, postprocess_inference=False
    )


def _create_w2v_alignment_config() -> AlignmentConfig:
    """Create alignment configuration for W2V phoneme-level alignment.

    Returns:
        Configured AlignmentConfig instance for W2V alignment.
    """
    return AlignmentConfig(False, False, False, True, False, WordSelectionStrategy.ENSEMBLE, False)


def _convert_alignment_to_matches(
    *, aligned_story: list[str], aligned_transcript: list[str]
) -> list[int]:
    """Convert aligned word sequences to binary match scores.

    Args:
        aligned_story: Aligned story words (may contain gaps).
        aligned_transcript: Aligned transcript words (may contain gaps).

    Returns:
        List of binary scores (1 for match, 0 for no match) for each story word.
    """
    matches: list[int] = []
    for story_word, transcript_word in zip(aligned_story, aligned_transcript):
        if story_word != ALIGNMENT_GAP_MARKER:
            if transcript_word != ALIGNMENT_GAP_MARKER and story_word == transcript_word:
                matches.append(WORD_MATCH_SCORE)
            else:
                matches.append(WORD_NO_MATCH_SCORE)
    return matches


def _convert_words_to_amirabet(*, words: list[str], phoneme_dict: dict[str, str]) -> str:
    """Convert list of words to Amirabet phoneme representation.

    Args:
        words: List of words to convert.
        phoneme_dict: Dictionary mapping words to phonemes.

    Returns:
        Space-separated Amirabet phoneme string.
    """
    amirabets = [english2amirabet(text_to_phons=phoneme_dict, word=word) for word in words]
    return " ".join(amirabets)


def _apply_phoneme_matching_corrections(
    *,
    matches: list[int],
    story_aligned: list[str],
    transcript_aligned: list[str],
    word_phoneme_distance: list[float],
) -> list[int]:
    """Apply phoneme-based matching corrections to improve alignment accuracy.

    Args:
        matches: Initial binary match scores.
        story_aligned: Aligned story words.
        transcript_aligned: Aligned transcript words.
        word_phoneme_distance: Phoneme distance scores for each word pair.

    Returns:
        Corrected binary match scores.
    """
    from src.orf_rescoring_pipeline.utils.file_operations import phoneme_match_with_map

    corrected_matches = matches.copy()

    for i, (story_word, transcript_word, dist) in enumerate(
        zip(story_aligned, transcript_aligned, word_phoneme_distance)
    ):
        if dist > EXACT_PHONEME_DISTANCE:
            if story_word == ALIGNMENT_GAP_MARKER or transcript_word == ALIGNMENT_GAP_MARKER:
                continue

            if phoneme_match_with_map(expected_word=story_word, transcribed_word=transcript_word):
                corrected_matches[i] = WORD_MATCH_SCORE
            else:
                corrected_matches[i] = WORD_NO_MATCH_SCORE

    return corrected_matches


def get_word_level_transcript_alignment(*, story_phrase: str, transcript_text: str) -> list[int]:
    """Get word-level alignment matches between story phrase and transcript.

    Uses the amira_fe word-level alignment algorithm to match words between
    the expected story phrase and the actual transcript text.

    Args:
        story_phrase: The expected phrase from the story.
        transcript_text: Transcribed text to align against.

    Returns:
        List of binary scores (1 for match, 0 for no match) for each word
        in the story phrase.
    """
    if not story_phrase or not transcript_text:
        return [WORD_NO_MATCH_SCORE] * len(story_phrase.split())

    try:
        config = _create_standard_alignment_config()

        alignment_result = align_texts_dash(
            config=config,
            lang=LanguageHandling.ENGLISH,
            next_exp=None,
            text1=story_phrase,
            text2=transcript_text,
        )

        return _convert_alignment_to_matches(
            aligned_story=alignment_result.aligned_reference,
            aligned_transcript=alignment_result.aligned_transcription_features,
        )

    except Exception as e:
        logger.warning(f"Error in alignment: {e}")
        return [WORD_NO_MATCH_SCORE] * len(story_phrase.split())


def get_word_level_transcript_alignment_w2v(
    *, story_phrase: str, transcript_text: str
) -> list[int]:
    """Get word-level alignment matches using W2V phoneme-level alignment.

    Uses phoneme-level alignment with Amirabet conversion and phoneme matching
    to provide more robust alignment for W2V transcripts.

    Args:
        story_phrase: The expected phrase from the story.
        transcript_text: Transcribed text to align against.

    Returns:
        List of binary scores (1 for match, 0 for no match) for each word
        in the story phrase.
    """
    if not story_phrase or not transcript_text:
        return [WORD_NO_MATCH_SCORE] * len(story_phrase.split())

    config = _create_w2v_alignment_config()

    phoneme_dict = _load_phoneme_dict_from_s3_sync()

    story_words = story_phrase.split()
    story_in_amirabet = _convert_words_to_amirabet(words=story_words, phoneme_dict=phoneme_dict)

    _, story_aligned, transcript_aligned, word_phoneme_distance, _ = phoneme_align_dash(
        story_phone=story_in_amirabet,
        transcript_phone=transcript_text,
        config=config,
    )

    initial_matches = [
        WORD_MATCH_SCORE if dist == EXACT_PHONEME_DISTANCE else WORD_NO_MATCH_SCORE
        for dist in word_phoneme_distance
    ]

    return _apply_phoneme_matching_corrections(
        matches=initial_matches,
        story_aligned=story_aligned,
        transcript_aligned=transcript_aligned,
        word_phoneme_distance=word_phoneme_distance,
    )
