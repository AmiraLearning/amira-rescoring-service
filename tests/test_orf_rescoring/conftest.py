"""Pytest configuration and fixtures for the ORF rescoring pipeline tests."""

import json
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import boto3
import pytest

from src.orf_rescoring_pipeline.models import (
    Activity,
    ModelFeature,
    PageData,
    TranscriptItem,
    WordItem,
)
from src.orf_rescoring_pipeline.utils.transcription import KaldiASRClient, W2VASRClient

# Mock boto3 at import time to prevent AWS calls and credential lookups

original_boto3_client = boto3.client
boto3.client = lambda service_name, **kwargs: MagicMock()  # type: ignore[assignment]


@pytest.fixture
def sample_word_items() -> list[WordItem]:
    """Create sample WordItem objects for testing."""
    return [
        WordItem("hello", 1.0, 1.5, 0.95),
        WordItem("world", 1.5, 2.0, 0.88),
        WordItem("how", 2.5, 2.8, 0.92),
        WordItem("are", 2.8, 3.0, 0.85),
        WordItem("you", 3.0, 3.5, 0.90),
    ]


@pytest.fixture
def sample_transcript_item(sample_word_items: list[WordItem]) -> TranscriptItem:
    """Create a sample TranscriptItem for testing."""
    return TranscriptItem(transcript="hello world how are you", words=sample_word_items)


@pytest.fixture
def sample_page_data() -> PageData:
    """Create sample PageData for testing."""
    return PageData(
        page_index=0,
        phrase_indices=[0, 1, 2],
        phrases=["hello world", "how are you", "good morning"],
        start_time=1000,
        end_time=5000,
        aligned_phrases=[
            {"phrase_idx": 0, "start": 1000, "end": 2000, "parsed": "hello world"},
            {"phrase_idx": 1, "start": 2500, "end": 3500, "parsed": "how are you"},
            {"phrase_idx": 2, "start": 4000, "end": 5000, "parsed": "good morning"},
        ],
    )


@pytest.fixture
def sample_model_feature() -> ModelFeature:
    """Create a sample ModelFeature for testing."""
    return ModelFeature(
        model="test_model_v1",
        phrase_index=0,
        kaldi_match=[1, 1, 0, 1, 0],
        w2v_match=[1, 0, 1, 1, 1],
        correct_confidences=[0.1, 0.9, 0.3, 0.8, 0.2],
        deepgram_match=[1, 1, 1, 0, 0],
    )


@pytest.fixture
def sample_activity(
    sample_page_data: PageData,
    sample_transcript_item: TranscriptItem,
    sample_model_feature: ModelFeature,
) -> Activity:
    """Create a sample Activity for testing."""
    activity = Activity(
        activity_id="test_activity_123",
        story_id=uuid.uuid4(),
        phrases=[
            "hello world how are you",
            "the quick brown fox",
            "good morning sunshine",
            "testing phrase alignment",
        ],
        pages=[sample_page_data],
        errors=[[True, False, True, False, True]],
        story_tags=["KALDI_SHARD_AMIRA_GENERAL_2"],
        transcript_json=sample_transcript_item,
        model_features=[sample_model_feature],
    )
    return activity


@pytest.fixture
def mock_audio_data() -> bytes:
    """Create mock audio data for testing."""
    # This is a minimal WAV header + some dummy data
    wav_header = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
    dummy_audio_data = b"\x00\x01" * 1024  # 2048 bytes of dummy audio
    return wav_header + dummy_audio_data


@pytest.fixture
def mock_kaldi_client() -> Mock:
    """Create a mock KaldiASRClient for testing."""
    mock_client = Mock(spec=KaldiASRClient)
    mock_result = Mock()
    mock_result.transcript = "hello world test"
    mock_client.transcribe.return_value = mock_result
    return mock_client


@pytest.fixture
def mock_w2v_client() -> Mock:
    """Create a mock W2VASRClient for testing."""
    mock_client = Mock(spec=W2VASRClient)
    mock_result = Mock()
    mock_result.transcript = "hello world test"
    mock_client.transcribe.return_value = mock_result
    return mock_client


@pytest.fixture(autouse=True)
def mock_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock external dependencies that are not available in test environment."""

    # Mock S3 operations
    def mock_s3_download(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("amira_pyutils.services.s3.s3_try_download_file", mock_s3_download)

    # boto3 is already mocked at module level to handle import-time client creation

    # Mock get_model_threshold to avoid S3 calls
    def mock_get_model_threshold(model: str) -> float:
        return 0.55  # Return a reasonable threshold value

    monkeypatch.setattr(
        "src.orf_rescoring_pipeline.models.get_model_threshold", mock_get_model_threshold
    )

    # Patch load_page_data to use test input data directory for integration tests
    from src.orf_rescoring_pipeline.models import Activity

    original_load_page_data = Activity.load_page_data

    def patched_load_page_data(self: Activity, *, page_data_dir: Path | str | None = None) -> None:
        if page_data_dir is None:
            # Use test input data directory for integration tests
            test_input_dir = Path(__file__).parent / "input_data" / "no_alt_phrases"
            page_data_dir = test_input_dir
        normalized_dir: Path
        if isinstance(page_data_dir, Path):
            normalized_dir = page_data_dir
        else:
            normalized_dir = Path(page_data_dir)

        original_load_page_data(self, page_data_dir=normalized_dir)

    monkeypatch.setattr(Activity, "load_page_data", patched_load_page_data)

    def mock_load_phoneme_dict_from_s3() -> dict[str, str]:
        """
        Mocked version of load_phoneme_dict_from_s3 for testing.

        Loads the phoneme dictionary from the test input_data/mock_all_story_words.dic file.
        """
        mock_dic_path = Path(__file__).parent / "input_data" / "mock_all_story_words.dic"
        if not mock_dic_path.exists():
            raise FileNotFoundError(
                f"Mock phoneme dictionary file not found at: {mock_dic_path}\n"
                f"Please ensure the file exists for tests."
            )
        phoneme_dict = {}
        with open(mock_dic_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    word, phonemes = parts
                    phoneme_dict[word] = phonemes
        return phoneme_dict

    monkeypatch.setattr(
        "src.orf_rescoring_pipeline.alignment.word_alignment._load_phoneme_dict_from_s3",
        mock_load_phoneme_dict_from_s3,
    )


# To add more activities:
# 1. Pull the required data for the activity:
#    - metadata as in the <activity_id>.json file - this file contains data from SRS, expected retouched errors, and kaldi and w2v transcriptions for the phrases that need to be rescored
#    - raw deepgram transcription files
#    - timing files - this file contains the timing information as a result of page-based phrase slicing and alignment using deepgram transcription
#    - phrase manifest - create the manifest using phrase_manifest.py and add it to the input_data/test_manifest.py file
# 2. Add the activity ID to the activity_ids list in each of the fixtures below
# 3. Write your tests for that activity using the fixtures


@pytest.fixture
def real_activities() -> dict[str, Activity]:
    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]
    activities = {}
    for activity_id in activity_ids:
        activities[activity_id] = _create_real_activity(activity_id)
    return activities


def _create_real_activity(activity_id: str) -> Activity:
    """Create Activity from real data files."""
    input_data_dir = Path(__file__).parent / "input_data"

    # Check if activity file exists
    activity_file = input_data_dir / f"{activity_id}.json"
    if not activity_file.exists():
        raise AssertionError(
            f"Activity data file not found: {activity_file}. "
            f"Required for integration tests with real activity data."
        )

    # Load real activity JSON
    with open(activity_file) as f:
        activity_data = cast(dict[str, Any], json.load(f))

    # Validate activity data structure
    required_keys = ["activityId", "storyId", "story", "model_features"]
    missing_keys = [key for key in required_keys if key not in activity_data]
    if missing_keys:
        raise AssertionError(
            f"Activity data in {activity_file} missing required keys: {missing_keys}. "
            f"Found keys: {list(activity_data.keys())}"
        )

    # Validate model features
    if not activity_data["model_features"] or len(activity_data["model_features"]) == 0:
        raise AssertionError(
            f"Activity {activity_id}: empty model_features array. Expected at least one model feature."
        )

    if not activity_data["errors"]:
        raise AssertionError(
            f"Activity {activity_id}: errors array not found. Expected errors array."
        )

    if not activity_data["errors_retouched"]:
        raise AssertionError(
            f"Activity {activity_id}: errors_retouched array not found. Expected errors_retouched array."
        )

    model_features_data = cast(list[dict[str, Any]], activity_data["model_features"])
    story_data = cast(dict[str, Any], activity_data["story"])
    phrases = cast(list[str], story_data["phrases"])
    errors = cast(list[list[bool]], activity_data["errors"])
    story_tags = cast(list[str], story_data.get("story_tags", []))

    # Create activity from activity_data directly instead of using async from_appsync_res
    activity = Activity(
        activity_id=activity_data["activityId"],
        story_id=uuid.UUID(activity_data["storyId"]),
        phrases=phrases,
        pages=[],  # Will be populated later
        errors=errors,
        story_tags=story_tags,
    )

    deepgram_matches: list[list[int]] = [
        cast(list[int], f["deepgram_matches"])
        for f in model_features_data
        if f["model"].find("second") != -1
    ]
    activity.ground_truth_deepgram_matches = deepgram_matches  # type: ignore[attr-defined]

    retouched_errors = cast(list[list[bool]], activity_data["errors_retouched"])
    activity.ground_truth_retouched_errors = retouched_errors  # type: ignore[attr-defined]

    resliced_kaldi_matches: list[list[int]] = [
        cast(list[int], f["kaldi_matches_new"])
        for f in model_features_data
        if f["model"].find("second") != -1
    ]
    activity.ground_truth_resliced_kaldi_matches = resliced_kaldi_matches  # type: ignore[attr-defined]

    resliced_w2v_matches: list[list[int]] = [
        cast(list[int], f["w2v_matches_new"])
        for f in model_features_data
        if f["model"].find("second") != -1
    ]
    activity.ground_truth_resliced_w2v_matches = resliced_w2v_matches  # type: ignore[attr-defined]

    return activity


@pytest.fixture
def real_model_features() -> dict[str, list[dict[str, Any]]]:
    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]
    model_features: dict[str, list[dict[str, Any]]] = {}
    for activity_id in activity_ids:
        model_features[activity_id] = _real_model_features(activity_id)
    return model_features


def _real_model_features(activity_id: str) -> list[dict[str, Any]]:
    """Real model features from model_features/ folder"""
    input_data_dir = Path(__file__).parent / "input_data"
    input_data_file = input_data_dir / f"{activity_id}.json"
    with open(input_data_file) as f:
        raw_model_features = cast(dict[str, Any], json.load(f))
    if "model_features" not in raw_model_features:
        raise ValueError(
            f"Model features not found in {input_data_file}. "
            f"Expected model_features key. "
            f"Found keys: {list(raw_model_features.keys())}"
        )
    result = cast(list[dict[str, Any]], raw_model_features["model_features"])
    return result


@pytest.fixture
def real_deepgram_transcripts() -> dict[str, dict[str, Any]]:
    """Real Deepgram transcripts from transcript files"""
    input_data_dir = Path(__file__).parent / "input_data"
    transcripts: dict[str, dict[str, Any]] = {}

    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]

    for activity_id in activity_ids:
        transcript_file = input_data_dir / f"{activity_id}_deepgram_transcript.json"
        if transcript_file.exists():
            with open(transcript_file) as f:
                data = cast(dict[str, Any], json.load(f))

                if "transcript" in data and "words" in data:
                    transcripts[activity_id] = data
                else:
                    raise AssertionError(
                        f"No transcript found in {transcript_file}. "
                        f"Expected 'transcript' key and 'words' key. "
                        f"Found keys: {list(data.keys())}"
                    )
        else:
            raise AssertionError(
                f"Deepgram transcript file not found: {transcript_file}. "
                f"Required for integration tests with real data."
            )

    return transcripts


@pytest.fixture
def real_kaldi_transcripts() -> dict[str, list[str]]:
    """Real Kaldi transcripts from transcript files"""
    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]
    kaldi_transcripts: dict[str, list[str]] = {}
    for activity_id in activity_ids:
        kaldi_transcripts[activity_id] = _real_transcript(activity_id, "kaldi")
    return kaldi_transcripts


@pytest.fixture
def real_w2v_transcripts() -> dict[str, list[str]]:
    """Real W2V transcripts from transcript files"""
    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]
    w2v_transcripts: dict[str, list[str]] = {}
    for activity_id in activity_ids:
        w2v_transcripts[activity_id] = _real_transcript(activity_id, "w2v")
    return w2v_transcripts


def _real_transcript(activity_id: str, transcript_type: str) -> list[str]:
    input_data_dir = Path(__file__).parent / "input_data"
    features_file = input_data_dir / f"{activity_id}.json"
    transcript_type = f"{transcript_type}_transcript".lower()
    with open(features_file) as f:
        model_features = cast(dict[str, Any], json.load(f))
    if transcript_type not in model_features:
        raise ValueError(
            f"{transcript_type} transcript not found in {features_file}. "
            f"Expected {transcript_type} key. "
            f"Found keys: {list(model_features.keys())}"
        )
    result = cast(list[str], model_features[transcript_type])
    return result


@pytest.fixture
def real_manifest_pages() -> dict[str, list[Any]]:
    """Real manifest pages from phrase manifest files"""
    import tests.test_orf_rescoring.input_data.test_manifest as tm

    manifest_pages: dict[str, list[Any]] = {}

    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]

    for activity_id in activity_ids:
        manifest = tm.get_activity_manifest(activity_id)
        manifest_pages[activity_id] = manifest

    return manifest_pages


@pytest.fixture
def real_timing_files() -> dict[str, dict[str, Any]]:
    """Real timing files from timing files folder"""
    timing_files: dict[str, dict[str, Any]] = {}
    activity_ids = ["jane_goodall_full_rescore", "jane_goodall_bad_needs_rescore"]
    for activity_id in activity_ids:
        timing_files[activity_id] = _real_timing_file(activity_id)
    return timing_files


def _real_timing_file(activity_id: str) -> dict[str, Any]:
    """Real timing file from timing files folder"""
    input_data_dir = Path(__file__).parent / "input_data"
    timing_file = input_data_dir / f"{activity_id}_timing_deepgram.json"
    with open(timing_file) as f:
        timing_data = cast(dict[str, Any], json.load(f))
    return timing_data
