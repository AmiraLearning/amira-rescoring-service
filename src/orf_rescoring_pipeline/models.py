"""Models for the ORF rescoring pipeline.

This module contains the core data structures used throughout the pipeline,
including Activity, ModelFeature, and supporting classes for handling
story content, audio data, and processing results.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, TYPE_CHECKING, cast

import aioboto3
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Constants
MODEL_METADATA_BUCKET: Final[str] = "amira-ml-models"
MODEL_METADATA_KEY_TEMPLATE: Final[str] = "{model}/assets/metadata.json"
MODEL_THRESHOLD_CACHE_SIZE: Final[int] = 32
DEFAULT_PAGE_DATA_DIR: Final[str] = "no_alt_phrases"
PUNCTUATION_PATTERN: Final[str] = r"[.,?!:;()\[\]\"'\u2019\u201c\u201d]"

# Story tag constants
KALDI_GENERAL_TAG: Final[str] = "KALDI_SHARD_AMIRA_GENERAL_2"
KALDI_PARTNER_TAG: Final[str] = "KALDI_SHARD_PARTNER_1"

# Retry configuration
RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_MIN_WAIT: Final[float] = 1.0
RETRY_MAX_WAIT: Final[float] = 10.0


@retry(  # type: ignore[misc]
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
    reraise=True,
)
async def _fetch_model_metadata(*, model: str) -> dict[str, Any]:
    """Fetch model metadata from S3 with retry logic.

    Args:
        model: The model name to fetch metadata for.

    Returns:
        The model metadata dictionary.

    Raises:
        Exception: If S3 operation fails after all retries.
    """
    s3_client = aioboto3.Session().client("s3")
    key = MODEL_METADATA_KEY_TEMPLATE.format(model=model)
    # Cast client for typed access to get_object
    client_any = cast(Any, s3_client)
    response = await client_any.get_object(Bucket=MODEL_METADATA_BUCKET, Key=key)
    data = json.loads(response["Body"].read().decode("utf-8"))
    return cast(dict[str, Any], data)


@lru_cache(maxsize=MODEL_THRESHOLD_CACHE_SIZE)
async def get_model_threshold(model: str) -> float:
    """Get the threshold value for a specific model.

    Args:
        model: The model name to get threshold for.

    Returns:
        The threshold value for the model.

    Raises:
        KeyError: If threshold not found in model metadata.
        Exception: If S3 operation fails.
    """
    metadata = await _fetch_model_metadata(model=model)
    return float(metadata["properties"]["threshold"])


@dataclass
class WordItem:
    """Individual word from transcript with timing and confidence data.

    Attributes:
        word: The transcribed word text.
        start: Start time in seconds.
        end: End time in seconds.
        confidence: Confidence score for the word.
    """

    word: str
    start: float
    end: float
    confidence: float

    def __str__(self) -> str:
        """Return string representation of the word item."""
        return f"{self.word}: {self.start} - {self.end} ({self.confidence})"


@dataclass
class TranscriptItem:
    """Complete transcript with word-level timing information.

    Attributes:
        transcript: The full transcript text.
        words: List of individual word items with timing.
    """

    transcript: str
    words: list[WordItem]


@dataclass
class PageData:
    """Data for a single page including phrases and timing information.

    Attributes:
        page_index: Zero-based page index.
        phrase_indices: Absolute indices in the story.
        phrases: The actual phrase text for this page.
        start_time: Start time in milliseconds from manifest.
        end_time: End time in milliseconds from manifest.
        aligned_phrases: Aligned phrase timings.
    """

    page_index: int
    phrase_indices: list[int]
    phrases: list[str]
    start_time: int | None = None
    end_time: int | None = None
    aligned_phrases: list[dict[str, Any]] | None = None


@dataclass
class ModelFeature:
    """Data for a single model feature record.

    Query returns features at phrase and model level.

    Attributes:
        model: The model name.
        phrase_index: Index of the phrase.
        kaldi_match: Kaldi match results.
        w2v_match: Wav2Vec match results.
        correct_confidences: Confidence scores for correct predictions.
        deepgram_match: Deepgram match results.
        resliced_kaldi_match: Resliced Kaldi match results.
        resliced_w2v_match: Resliced Wav2Vec match results.
    """

    model: str
    phrase_index: int
    kaldi_match: list[int] = field(default_factory=list)
    w2v_match: list[int] = field(default_factory=list)
    correct_confidences: list[float] = field(default_factory=list)
    deepgram_match: list[int] = field(default_factory=list)
    resliced_kaldi_match: list[int] = field(default_factory=list)
    resliced_w2v_match: list[int] = field(default_factory=list)

    @staticmethod
    def from_json(json_data: dict[str, Any]) -> "ModelFeature":
        """Create ModelFeature from JSON data.

        Args:
            json_data: Dictionary containing model feature data.

        Returns:
            ModelFeature instance.
        """
        w2v_match = (
            json_data["W2V_match"]
            if json_data["W2V_match"] is not None
            else [d == 0 for d in json_data["we_dist"]]
        )

        return ModelFeature(
            model=json_data["model"],
            phrase_index=int(json_data["phraseIndex"]),
            kaldi_match=json_data["Kaldi_match"],
            w2v_match=w2v_match,
            correct_confidences=json_data["correct_confidences"],
        )

    def __str__(self) -> str:
        """Return string representation of the model feature."""
        return (
            f"{self.model=} {self.phrase_index=} "
            f"{len(self.deepgram_match)=} {len(self.correct_confidences)=}"
        )


@dataclass
class Activity:
    """Complete activity data including story content, audio, and processing results.

    This is the main data structure that flows through the pipeline, accumulating
    data at each processing step.

    Attributes:
        activity_id: Unique identifier for the activity.
        story_id: UUID of the associated story.
        phrases: List of story phrases.
        pages: List of page data objects.
        errors: Original error predictions.
        story_tags: Tags associated with the story.
        complete_audio_s3_path: S3 path to complete audio file.
        transcript_json: Transcript data with timing.
        audio_file_data: Raw audio file bytes.
        timing_path: Path to timing data.
        model_features: List of model feature data.
        errors_retouched: Retouched error predictions.
    """

    activity_id: str
    story_id: uuid.UUID
    phrases: list[str]
    pages: list[PageData] = field(default_factory=list)
    errors: list[list[bool]] = field(default_factory=list)
    story_tags: list[str] = field(default_factory=list)
    # TODO(amira_pyutils): Replace Any with S3Addr from amira_pyutils.services.s3 when available
    complete_audio_s3_path: Any | None = None
    transcript_json: TranscriptItem | None = None
    audio_file_data: bytes | None = None
    timing_path: str | None = None
    model_features: list[ModelFeature] = field(default_factory=list)
    errors_retouched: list[list[bool]] = field(default_factory=list)

    @staticmethod
    async def from_appsync_res(appsync_res: list[dict[str, Any]]) -> list["Activity"]:
        """Create Activity objects from AppSync response data.

        Args:
            appsync_res: Response data from AppSync query.

        Returns:
            List of Activity objects with basic story data loaded.
        """
        return [
            await Activity._create_from_activity_data(activity_data=activity_data)
            for activity_data in appsync_res
        ]

    @staticmethod
    async def _create_from_activity_data(*, activity_data: dict[str, Any]) -> "Activity":
        """Create single Activity from activity data.

        Args:
            activity_data: Single activity data dictionary.

        Returns:
            Activity instance.
        """
        phrases = await Activity._normalize_phrases(
            phrases=activity_data["story"]["chapters"][0]["phrases"]
        )

        return Activity(
            activity_id=activity_data["activityId"],
            story_id=uuid.UUID(activity_data["storyId"]),
            phrases=phrases,
            errors=activity_data["errors"],
            story_tags=activity_data["story"]["tags"],
        )

    @staticmethod
    async def _normalize_phrases(*, phrases: list[str]) -> list[str]:
        """Normalize phrases by removing punctuation and converting to lowercase.

        Args:
            phrases: List of raw phrase strings.

        Returns:
            List of normalized phrases.
        """
        return [re.sub(PUNCTUATION_PATTERN, "", phrase.lower()) for phrase in phrases]

    async def model_features_from_appsync_res(
        self, *, appsync_model_res: list[dict[str, Any]]
    ) -> None:
        """Load model features from AppSync response.

        Args:
            appsync_model_res: Model feature data from AppSync.
        """
        deduped_features = self._deduplicate_model_features(features=appsync_model_res)
        self.model_features = sorted(deduped_features, key=lambda x: x.phrase_index)

    def _deduplicate_model_features(self, *, features: list[dict[str, Any]]) -> list[ModelFeature]:
        """Deduplicate model features based on phrase index and model.

        Args:
            features: Raw feature data from AppSync.

        Returns:
            List of deduplicated ModelFeature objects.
        """
        seen = set()
        deduped_features = []

        for feature in features:
            if self._should_skip_feature(feature=feature):
                continue

            key = (feature.get("phraseIndex"), feature.get("model"))
            if key not in seen and feature.get("correct_confidences") is not None:
                seen.add(key)
                deduped_features.append(ModelFeature.from_json(feature))

        return deduped_features

    def _should_skip_feature(self, *, feature: dict[str, Any]) -> bool:
        """Check if a feature should be skipped during processing.

        Args:
            feature: Feature data dictionary.

        Returns:
            True if feature should be skipped.
        """
        model_val = str(feature.get("model", ""))
        return model_val.lower().startswith("faux")

    @property
    def second_pass_model_features(self) -> list[ModelFeature]:
        """Get second pass model features.

        Returns:
            Sorted list of second pass model features.
        """
        return sorted(
            [feature for feature in self.model_features if "second" in feature.model.lower()],
            key=lambda x: x.phrase_index,
        )

    @property
    def first_pass_model_features(self) -> list[ModelFeature]:
        """Get first pass model features.

        Returns:
            Sorted list of first pass model features.
        """
        return sorted(
            [feature for feature in self.model_features if "second" not in feature.model.lower()],
            key=lambda x: x.phrase_index,
        )

    @property
    def preferred_model_features(self) -> list[ModelFeature]:
        """Get preferred model features.

        Prefers second pass features if complete, otherwise uses first pass.

        Returns:
            List of preferred model features.
        """
        second_pass = self.second_pass_model_features
        if len(second_pass) > 0 and len(second_pass) == self.max_feature_index + 1:
            return second_pass
        return self.first_pass_model_features

    @property
    def max_feature_index(self) -> int:
        """Get the maximum feature index.

        Returns:
            Maximum phrase index from model features.
        """
        return max([feature.phrase_index for feature in self.model_features])

    @property
    def models(self) -> list[str]:
        """Get all unique model names.

        Returns:
            List of unique model names.
        """
        return list({feature.model for feature in self.model_features})

    @property
    def model_predictions(self) -> list[list[bool]]:
        """Get model predictions based on confidence thresholds.

        Returns:
            List of boolean predictions for each phrase.
        """

        def _get_model_threshold_sync(model: str) -> float:
            """Sync bridge to async threshold call.

            TODO(network): Replace with upstream sync/cache layer when available.
            """
            try:
                import asyncio

                return float(asyncio.run(get_model_threshold(model)))
            except Exception:
                # Fallback to a safe default threshold
                return 0.5

        return [
            [
                _get_model_threshold_sync(feature.model) is not None
                and conf < _get_model_threshold_sync(feature.model)
                for conf in feature.correct_confidences
            ]
            for feature in self.preferred_model_features
        ]

    @property
    def is_kaldi_general(self) -> bool:
        """Check if the story is a Kaldi general story.

        Returns:
            True if story has Kaldi general tag.
        """
        return KALDI_GENERAL_TAG in self.story_tags

    @property
    def is_kaldi_partner(self) -> bool:
        """Check if the story is a Kaldi partner story.

        Returns:
            True if story has Kaldi partner tag.
        """
        return KALDI_PARTNER_TAG in self.story_tags

    def load_page_data(self, *, page_data_dir: Path | None = None) -> None:
        """Load page text files and group phrases by pages.

        Reads page files from page_data_dir/<story_id>_<page_num>_page.txt
        and matches story phrases to page content.

        Args:
            page_data_dir: Directory containing page files.
                          Defaults to ~/no_alt_phrases if not provided.
        """
        logger.debug(f"Activity {self.activity_id}: Loading page data for story {self.story_id}")

        page_dir = self._get_page_directory(page_data_dir=page_data_dir)
        if not self._validate_page_directory(page_dir=page_dir):
            return

        pages = self._load_pages_from_directory(page_dir=page_dir)
        self.pages = sorted(pages, key=lambda x: x.page_index)

        logger.debug(
            f"Activity {self.activity_id}: Loaded {len(pages)} pages with "
            f"total {sum(len(p.phrases) for p in pages)} phrases"
        )

    def _get_page_directory(self, *, page_data_dir: Path | None) -> Path:
        """Get the page data directory path.

        Args:
            page_data_dir: Optional directory path.

        Returns:
            Path to page data directory.
        """
        if page_data_dir:
            logger.debug(
                f"Activity {self.activity_id}: Using provided page directory: {page_data_dir}"
            )
            return page_data_dir

        return Path.home() / DEFAULT_PAGE_DATA_DIR

    def _validate_page_directory(self, *, page_dir: Path) -> bool:
        """Validate that page directory exists.

        Args:
            page_dir: Path to page directory.

        Returns:
            True if directory exists.
        """
        if not page_dir.exists():
            logger.warning(f"Activity {self.activity_id}: Page directory {page_dir} does not exist")
            return False
        return True

    def _load_pages_from_directory(self, *, page_dir: Path) -> list[PageData]:
        """Load all pages from the directory.

        Args:
            page_dir: Path to page directory.

        Returns:
            List of loaded PageData objects.
        """
        pages = []
        page_files = page_dir.glob(f"{self.story_id}_*_page.txt")

        for page_file in page_files:
            try:
                page_data = self._load_single_page(page_file=page_file)
                if page_data:
                    pages.append(page_data)
            except Exception as e:
                logger.error(
                    f"Activity {self.activity_id}: Error reading page file {page_file}: {e}"
                )
                break

        return pages

    def _load_single_page(self, *, page_file: Path) -> PageData | None:
        """Load a single page file.

        Args:
            page_file: Path to page file.

        Returns:
            PageData object if successful, None otherwise.
        """
        page_num = int(page_file.name.split("_")[1])

        with open(page_file, encoding="utf-8") as f:
            page_words = [line.strip() for line in f.readlines() if line.strip()]

        logger.debug(f"Activity {self.activity_id}: Page {page_num} has {len(page_words)} words")

        page_phrases, phrase_indices = self._match_phrases_to_page(page_words=page_words)

        if not page_phrases:
            logger.warning(f"Activity {self.activity_id}: Page {page_num} matched no phrases")
            return None

        logger.debug(
            f"Activity {self.activity_id}: Page {page_num} matched {len(page_phrases)} phrases"
        )

        return PageData(
            page_index=page_num - 1,
            phrase_indices=phrase_indices,
            phrases=page_phrases,
        )

    def _match_phrases_to_page(self, *, page_words: list[str]) -> tuple[list[str], list[int]]:
        """Match story phrases to page words.

        Args:
            page_words: List of words on the page.

        Returns:
            Tuple of (matched_phrases, phrase_indices).
        """
        page_text = " ".join(word.lower() for word in page_words)
        page_phrases = []
        phrase_indices = []

        for i, phrase in enumerate(self.phrases):
            if self._phrase_matches_page(phrase=phrase, page_text=page_text):
                page_phrases.append(phrase)
                phrase_indices.append(i)

        return page_phrases, phrase_indices

    def _phrase_matches_page(self, *, phrase: str, page_text: str) -> bool:
        """Check if a phrase matches the page text.

        Args:
            phrase: The phrase to match.
            page_text: The page text to match against.

        Returns:
            True if phrase matches page text.
        """
        phrase_words = phrase.split()

        if not all(word in page_text for word in phrase_words):
            return False

        phrase_text = " ".join(phrase_words)
        return phrase_text in page_text

    def pad_to_phrase_length(self, *, errors: list[bool], phrase_index: int) -> list[bool]:
        """Pad errors to the length of the phrase.

        Args:
            errors: List of error flags.
            phrase_index: Index of the phrase.

        Returns:
            Padded error list.

        Raises:
            ValueError: If errors length exceeds phrase length.
        """
        phrase_length = len(self.preferred_model_features[phrase_index].correct_confidences)

        if len(errors) < phrase_length:
            return errors + [True] * (phrase_length - len(errors))
        elif len(errors) > phrase_length:
            raise ValueError(
                f"Errors length {len(errors)} is greater than phrase length "
                f"{self.preferred_model_features[phrase_index]}"
            )

        return errors

    def __str__(self) -> str:
        """Return string representation of the activity."""
        return (
            f"Activity: {self.activity_id}\n"
            f"\t Story ID: {self.story_id}\n"
            f"\t Story Phrases: {len(self.phrases or [])}\n"
            f"\t Story Pages: {len(self.pages or [])}\n"
            f"\t Has Transcript: {bool(self.transcript_json)}\n"
            f"\t Has Model Features: {bool(self.model_features)}\n"
            f"\t Has Errors: {bool(self.errors)}\n"
            f"\t Has deepgram matches: {bool(any(feature.deepgram_match for feature in self.model_features))}\n"
            f"\t Has errors retouched: {bool(self.errors_retouched)}\n"
        )
