"""
Audio extraction functionality for the offline pipeline.

This module handles both page-level and phrase-level audio extraction:
- Page-level: Extract audio segments for complete pages
- Phrase-level: Extract audio segments for individual aligned phrases

The extracted audio files are organized in separate directories for easy management.
"""

import logging
import uuid
from pathlib import Path

from pydub import AudioSegment

from orf_rescoring_pipeline.alignment.audio_processing import load_activity_audio_data_from_s3
from orf_rescoring_pipeline.models import Activity

logger = logging.getLogger(__name__)


def extract_page_audio(activity: Activity, manifest_pages: list) -> list[str]:
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

    # Use offline_pipeline directory for temp files
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

        page_files = []
        for page_data in manifest_pages:
            page_index = page_data.phrase  # This is the page index from manifest
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

    # Use offline_pipeline directory for temp files
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


def cli_extract_audio(activity_id: str, env_name: str = "prod") -> None:
    """
    CLI function to extract phrase and page audio files for a given activity ID.

    NOTE: this module is purely meant for debugging and exploration of the audio extraction process and is not used in the pipeline.py script.

    Args:
        activity_id: The activity ID to process
        env_name: Environment name (prod, stage, etc.)
    """
    import sys
    from pathlib import Path

    # Add parent directory to path for imports
    sys.path.insert(0, str(Path(__file__).parent.parent))

    import os

    import amira_pyutils.services.s3 as s3_utils
    from amira_pyutils.general.environment import Environments
    from dotenv import load_dotenv

    from orf_rescoring_pipeline.alignment.phrase_alignment import (
        process_activity_timing,
    )
    from orf_rescoring_pipeline.alignment.phrase_manifest import (
        PhraseBuilder,
        PhraseManifest,
    )
    from orf_rescoring_pipeline.models import Activity
    from orf_rescoring_pipeline.utils.logging import configure_logging
    from orf_rescoring_pipeline.utils.transcription import DeepgramASRClient

    # Load environment variables
    load_dotenv(Path(__file__).parent.parent / ".env")

    # Configure logging
    configure_logging(log_level="INFO", log_file=f"audio_extraction_{activity_id}.log")

    try:
        # Get environment
        env = Environments.find(env_name)

        # Create activity object
        activity = Activity(
            activity_id=activity_id,
            story_id=uuid.uuid4(),  # Temporary placeholder
            phrases=[],  # Will be loaded from S3
        )

        # Load audio data from S3
        activity.complete_audio_s3_path = s3_utils.s3_addr_from_uri(
            f"s3://{env.audio_bucket}/{activity_id}/complete.wav"
        )

        print(f"📥 Loading audio data for {activity_id} from {env.audio_bucket}...")
        load_activity_audio_data_from_s3(activity=activity, audio_bucket=env.audio_bucket)

        # Initialize Deepgram for transcription
        deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
        if not deepgram_api_key:
            raise ValueError("DEEPGRAM_API_KEY not found in environment variables")

        deepgram = DeepgramASRClient(api_key=deepgram_api_key)

        print("🎤 Transcribing audio with Deepgram...")
        activity.transcript_json = deepgram.transcribe(activity, save_transcript=True)

        if not activity.transcript_json:
            raise ValueError("Transcription failed")

        # Generate phrase manifest
        print("📋 Generating phrase manifest...")
        builder = PhraseBuilder(s3_client=s3_utils.get_client())
        manifest = PhraseManifest(builder=builder)
        pages = manifest.generate(bucket=env.audio_bucket, activity_id=activity_id)

        print(f"📄 Found {len(pages)} audio pages")

        # Process timing alignment
        print("⏱️  Processing timing alignment...")
        process_activity_timing(activity=activity, manifest_pages=pages, save_files=True)

        # Extract page-level audio
        print("🎵 Extracting page-level audio...")
        page_files = extract_page_audio(activity, pages)
        print(f"✅ Extracted {len(page_files)} page audio files:")
        for file_path in page_files:
            print(f"   📁 {file_path}")

        # Extract phrase-level audio
        print("🎯 Extracting phrase-level audio...")
        phrase_files = extract_phrase_audio(activity)
        print(f"✅ Extracted {len(phrase_files)} phrase audio files:")
        for file_path in phrase_files[:5]:  # Show first 5
            print(f"   📁 {file_path}")
        if len(phrase_files) > 5:
            print(f"   ... and {len(phrase_files) - 5} more files")

        # Show output directory
        pipeline_dir = Path(__file__).parent.parent
        output_dir = pipeline_dir / "temp" / activity_id
        print("\n🎉 Audio extraction complete!")
        print(f"📂 Output directory: {output_dir}")
        print(f"   ├── page_audio/     ({len(page_files)} files)")
        print(f"   └── phrase_audio/   ({len(phrase_files)} files)")

    except Exception as e:
        print(f"❌ Error extracting audio for {activity_id}: {e}")
        logger.error(f"CLI extraction failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import fire

    # Use Fire to create a CLI
    fire.Fire(
        {
            "extract": cli_extract_audio,
        }
    )
