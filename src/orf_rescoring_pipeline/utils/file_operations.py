"""File operations utilities for the ORF rescoring pipeline.

This module provides utilities for file operations including phoneme matching,
prediction trimming, and S3 upload operations.
"""

import csv
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final

import amira_pyutils.s3 as s3_utils
from amira_pyutils.logging import get_logger

logger = get_logger(__name__)

ACCEPTANCE_MAP: Final[MappingProxyType[str, set[str]]] = MappingProxyType(
    {
        "a": {"a"},
        "b": {"b", "v"},
        "d": {"d"},
        "f": {"f", "s"},
        "g": {"g"},
        "h": {"h"},
        "i": {"i"},
        "j": {"j"},
        "k": {"k"},
        "l": {"l", "w", "ɹ"},
        "m": {"m", "n", "ŋ"},
        "n": {"n", "m", "ŋ"},
        "o": {"o"},
        "p": {"p"},
        "s": {"s", "f", "ʃ"},
        "t": {"t"},
        "u": {"u"},
        "v": {"v", "z"},
        "w": {"w", "ɹ"},
        "x": {"x", "t"},
        "y": {"y"},
        "z": {"z", "v"},
        "æ": {"æ", "ɛ"},
        "ð": {"ð"},
        "ŋ": {"ŋ", "n", "m"},
        "ɑ": {"ɑ", "ɔ", "ʌ"},
        "ɔ": {"ɔ", "ɑ", "ʌ"},
        "ɛ": {"ɛ", "æ", "ɪ"},
        "ɝ": {"ɝ", "ɹ"},
        "ɪ": {"ɪ", "ɛ"},
        "ɹ": {"ɹ", "w", "ʌ", "ɝ"},
        "ʃ": {"ʃ", "s"},
        "ʊ": {"ʊ", "ʌ"},
        "ʌ": {"ʌ", "ɔ", "ʊ"},
        "α": {"α"},
        "γ": {"γ", "ɑ"},
        "θ": {"θ"},
        "ω": {"ω"},
    }
)


def phoneme_match_with_map(*, expected_word: str, transcribed_word: str) -> bool:
    """Check if transcribed word matches expected word using phoneme substitution map.

    Args:
        expected_word: The expected phonetic representation.
        transcribed_word: The actual transcribed phonetic representation.

    Returns:
        True if the transcribed word matches the expected word according to the
        phoneme acceptance map, False otherwise.
    """
    pattern_parts: list[str] = []
    for char in expected_word:
        substitutions = ACCEPTANCE_MAP.get(char, set())
        substitutions.add(char)
        escaped = [re.escape(c) for c in substitutions]
        joined_subs: str = "".join(escaped)
        pattern_parts.append("[" + joined_subs + "]")

    pattern_str = "".join(pattern_parts)
    compiled_pattern = re.compile(pattern_str)

    return bool(compiled_pattern.search(transcribed_word))


def trim_predictions(*, predictions: list[list[bool]]) -> list[list[bool]]:
    """Trim trailing errors from the last phrase of predictions.

    Walks the last phrase of predictions backwards and removes all errors until
    the first non-error is found. If the last phrase contains all errors,
    removes the phrase completely.

    Args:
        predictions: List of phrases, where each phrase is a list of boolean
            error indicators.

    Returns:
        Trimmed predictions with trailing errors removed from the last phrase.
    """
    try:
        last_phrase = predictions[-1]

        if len(last_phrase) == 1:
            return predictions

        flat_errors = [error for phrase in predictions for error in phrase]
        last_non_error = len(flat_errors) - list(reversed(flat_errors)).index(False)
        flat_errors = flat_errors[:last_non_error]

        trimmed_errors = []
        pointer = 0

        for arr in predictions:
            arr = flat_errors[pointer : pointer + len(arr)]
            pointer += len(arr)
            if len(arr) > 0:
                trimmed_errors.append(arr)

        return trimmed_errors

    except Exception as error:
        logger.warning("Could not trim last phrase %s", str(error))
        return predictions


async def upload_file_to_s3(
    *,
    file_path: str,
    bucket_name: str,
    s3_key_prefix: str,
    file_type: str = "file",
    cleanup_after_upload: bool = False,
) -> str | None:
    """Upload file to S3 and return the S3 URL.

    Args:
        file_path: Path to the file to upload.
        bucket_name: S3 bucket name.
        s3_key_prefix: Prefix for the S3 key.
        file_type: Type of file for logging purposes.
        cleanup_after_upload: If True, delete the original file after successful upload.

    Returns:
        S3 URL of the uploaded file or None if upload failed.
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = Path(file_path).name
        upload_dest = s3_utils.S3Address.from_uri(
            uri=f"s3://{bucket_name}/{s3_key_prefix}/{timestamp}_{filename}"
        )

        await s3_utils.S3Service().upload_file(
            bucket=upload_dest.bucket, key=upload_dest.key, filename=file_path
        )

        s3_url = f"s3://{upload_dest.bucket}/{upload_dest.key}"
        logger.info(f"Uploaded {file_type} to S3: {s3_url}")

        if cleanup_after_upload:
            try:
                os.unlink(file_path)
                logger.info(f"Cleaned up {file_type} after successful upload: {file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up {file_type} {file_path}: {cleanup_error}")

        return s3_url

    except Exception as e:
        logger.error(f"Failed to upload {file_type} to S3: {e}")
        return None


async def upload_logs_to_s3(
    *,
    log_file_path: str,
    bucket_name: str,
    s3_key_prefix: str = "orf-rescoring-pipeline/logs",
) -> str | None:
    """Upload log file to S3 and return the S3 URL.

    Deletes the original log file after successful upload.

    Args:
        log_file_path: Path to the log file.
        bucket_name: S3 bucket name.
        s3_key_prefix: Prefix for the S3 key.

    Returns:
        S3 URL of the uploaded file or None if upload failed.
    """
    return await upload_file_to_s3(
        file_path=log_file_path,
        bucket_name=bucket_name,
        s3_key_prefix=s3_key_prefix,
        file_type="log file",
        cleanup_after_upload=True,
    )


async def upload_csv_to_s3(
    *,
    activity_ids: set[str],
    env_name: str,
    bucket_name: str,
    s3_key_prefix: str = "orf-rescoring-pipeline/csv",
) -> str | None:
    """Create CSV from activity IDs and upload to S3 using a temporary file.

    Args:
        activity_ids: Set of activity IDs to include in CSV.
        env_name: Environment name for filename.
        bucket_name: S3 bucket name.
        s3_key_prefix: Prefix for the S3 key.

    Returns:
        S3 URL of the uploaded file or None if upload failed.
    """
    if not activity_ids:
        logger.warning("No activity IDs provided for CSV upload")
        return None

    try:
        csv_filename = f"activities_{env_name}.csv"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as temp_file:
            writer = csv.writer(temp_file)
            writer.writerow(["activity_id"])
            for activity_id in sorted(activity_ids):
                writer.writerow([activity_id])
            temp_file_path = temp_file.name

        logger.info(
            f"Created temporary CSV with {len(activity_ids)} activity IDs: {temp_file_path}"
        )

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            timestamped_filename = f"{timestamp}_{csv_filename}"
            upload_dest = s3_utils.S3Address.from_uri(
                uri=f"s3://{bucket_name}/{s3_key_prefix}/{timestamped_filename}"
            )

            await s3_utils.S3Service().upload_file(
                bucket=upload_dest.bucket, key=upload_dest.key, filename=temp_file_path
            )

            s3_url = f"s3://{upload_dest.bucket}/{upload_dest.key}"
            logger.info(f"Uploaded CSV file to S3: {s3_url}")
            return s3_url

        finally:
            os.unlink(temp_file_path)

    except Exception as e:
        logger.error(f"Failed to create or upload CSV file to S3: {e}")
        return None
