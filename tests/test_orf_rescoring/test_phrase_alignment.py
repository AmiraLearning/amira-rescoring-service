"""
Test suite for phrase alignment functionality.

This module tests the phrase-level alignment functions that match expected phrases
to transcribed tokens within page time boundaries, and timing file generation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TypedDict
from unittest.mock import Mock, patch

import pytest

from src.orf_rescoring_pipeline.alignment.phrase_alignment import (
    align_page_phrases,
    process_activity_timing,
    save_timing_file,
)
from src.orf_rescoring_pipeline.models import Activity, PageData, WordItem


class ScenarioDict(TypedDict):
    name: str
    phrases: list[str]
    tokens: list[WordItem]
    expected_phrases: int


@pytest.mark.unit
class TestAlignPagePhrases:
    """Test cases for align_page_phrases function."""

    def test_perfect_alignment(self: TestAlignPagePhrases) -> None:
        """Test alignment when phrases perfectly match tokens."""
        expected_phrases = ["hello world", "how are you"]
        tokens = [
            WordItem("hello", 1.0, 1.5, 0.9),
            WordItem("world", 1.5, 2.0, 0.8),
            WordItem("how", 2.5, 3.0, 0.85),
            WordItem("are", 3.0, 3.3, 0.92),
            WordItem("you", 3.3, 3.8, 0.88),
        ]
        page_start_ms = 1000
        page_end_ms = 4000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 2, "Should return 2 phrase boundaries"

        # First phrase: "hello world"
        assert boundaries[0]["phrase_idx"] == 0
        assert boundaries[0]["start"] == 1000  # hello start time in ms
        assert boundaries[0]["end"] == 2000  # world end time in ms
        assert boundaries[0]["parsed"] == "hello world"

        # Second phrase: "how are you"
        assert boundaries[1]["phrase_idx"] == 1
        assert boundaries[1]["start"] == 2500  # how start time in ms
        assert boundaries[1]["end"] == 3800  # you end time in ms
        assert boundaries[1]["parsed"] == "how are you"

    def test_partial_alignment(self: TestAlignPagePhrases) -> None:
        """Test alignment when some words are missing from tokens."""
        expected_phrases = ["hello world", "good morning"]
        tokens = [
            WordItem("hello", 1.0, 1.5, 0.9),
            # "world" is missing
            WordItem("good", 2.5, 3.0, 0.85),
            WordItem("morning", 3.0, 3.5, 0.88),
        ]
        page_start_ms = 1000
        page_end_ms = 4000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 2, "Should return 2 phrase boundaries"

        # First phrase: only "hello" matched
        assert boundaries[0]["phrase_idx"] == 0
        assert boundaries[0]["start"] == 1000
        assert boundaries[0]["end"] == 1500
        assert boundaries[0]["parsed"] == "hello"

        # Second phrase: "good morning" matched
        assert boundaries[1]["phrase_idx"] == 1
        assert boundaries[1]["start"] == 2500
        assert boundaries[1]["end"] == 3500
        assert boundaries[1]["parsed"] == "good morning"

    def test_no_matching_tokens(self: TestAlignPagePhrases) -> None:
        """Test alignment when no tokens match expected phrases."""
        expected_phrases = ["hello world", "good morning"]
        tokens = [
            WordItem("completely", 1.0, 1.5, 0.9),
            WordItem("different", 1.5, 2.0, 0.8),
            WordItem("words", 2.0, 2.5, 0.85),
        ]
        page_start_ms = 1000
        page_end_ms = 3000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 2, "Should return 2 phrase boundaries even with no matches"

        # Both phrases should have continuity enforced but may have some parsed text
        for boundary in boundaries:
            assert boundary["start"] is not None, "Start should be set to page_start_ms"
            assert boundary["end"] is not None, "End should be set"
            # Note: parsed text may contain partial matches from the alignment algorithm

    def test_empty_inputs(self: TestAlignPagePhrases) -> None:
        """Test alignment with empty inputs."""
        # Empty phrases
        boundaries = align_page_phrases(
            expected_phrases=[],
            tokens=[WordItem(word="hello", start=1.0, end=1.5, confidence=0.9)],
            page_start_ms=1000,
            page_end_ms=2000,
        )
        assert boundaries == [], "Empty phrases should return empty boundaries"

        # Empty tokens
        boundaries = align_page_phrases(
            expected_phrases=["hello world"], tokens=[], page_start_ms=1000, page_end_ms=2000
        )
        assert boundaries == [], "Empty tokens should return empty boundaries"

        # Both empty
        boundaries = align_page_phrases(
            expected_phrases=[], tokens=[], page_start_ms=1000, page_end_ms=2000
        )
        assert boundaries == [], "Both empty should return empty boundaries"

    def test_tokens_outside_page_range(self: TestAlignPagePhrases) -> None:
        """Test that tokens outside page time range are filtered out."""
        expected_phrases = ["hello world"]
        tokens = [
            WordItem("before", 0.5, 0.8, 0.9),  # Before page start
            WordItem("hello", 1.0, 1.5, 0.9),  # Within range
            WordItem("world", 1.5, 2.0, 0.8),  # Within range
            WordItem("after", 4.5, 5.0, 0.85),  # After page end
        ]
        page_start_ms = 1000  # 1.0 seconds
        page_end_ms = 3000  # 3.0 seconds

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 1
        assert boundaries[0]["parsed"] == "hello world", "Should only use tokens within time range"

    def test_continuity_enforcement(self: TestAlignPagePhrases) -> None:
        """Test that phrase boundaries maintain continuity within page."""
        expected_phrases = ["phrase one", "phrase two", "phrase three"]
        tokens = [
            WordItem("phrase", 1.0, 1.2, 0.9),
            WordItem("one", 1.2, 1.5, 0.8),
            # Gap in tokens - phrase two won't match
            WordItem("phrase", 3.0, 3.2, 0.85),
            WordItem("three", 3.2, 3.5, 0.88),
        ]
        page_start_ms = 1000
        page_end_ms = 4000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 3

        # Check that boundaries maintain continuity
        for i in range(len(boundaries) - 1):
            assert boundaries[i]["end"] <= boundaries[i + 1]["start"], (
                f"Boundary {i} end ({boundaries[i]['end']}) should not exceed "
                f"boundary {i + 1} start ({boundaries[i + 1]['start']})"
            )

    def test_single_word_phrases(self: TestAlignPagePhrases) -> None:
        """Test alignment with single word phrases."""
        expected_phrases = ["hello", "world", "test"]
        tokens = [
            WordItem("hello", 1.0, 1.5, 0.9),
            WordItem("world", 2.0, 2.5, 0.8),
            WordItem("test", 3.0, 3.5, 0.85),
        ]
        page_start_ms = 1000
        page_end_ms = 4000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 3

        for i, boundary in enumerate(boundaries):
            assert boundary["phrase_idx"] == i
            assert boundary["parsed"] == expected_phrases[i]

    def test_overlapping_time_ranges(self: TestAlignPagePhrases) -> None:
        """Test alignment when token time ranges overlap."""
        expected_phrases = ["hello world"]
        tokens = [
            WordItem("hello", 1.0, 2.0, 0.9),  # Overlaps with world
            WordItem("world", 1.5, 2.5, 0.8),  # Overlaps with hello
        ]
        page_start_ms = 1000
        page_end_ms = 3000

        boundaries = align_page_phrases(
            expected_phrases=expected_phrases,
            tokens=tokens,
            page_start_ms=page_start_ms,
            page_end_ms=page_end_ms,
        )

        assert len(boundaries) == 1
        assert boundaries[0]["parsed"] == "hello world"
        # Should use first token start and last token end
        assert boundaries[0]["start"] == 1000
        assert boundaries[0]["end"] == 2500

    def test_end_to_end_alignment_processing(self: TestAlignPagePhrases) -> None:
        """Test complete end-to-end phrase alignment processing."""
        # Create realistic test data
        phrases = ["once upon a time", "there was a princess", "in a far away land"]
        tokens = [
            WordItem("once", 1.0, 1.3, 0.95),
            WordItem("upon", 1.3, 1.6, 0.92),
            WordItem("a", 1.6, 1.7, 0.88),
            WordItem("time", 1.7, 2.1, 0.94),
            WordItem("there", 2.5, 2.8, 0.91),
            WordItem("was", 2.8, 3.0, 0.87),
            WordItem("a", 3.0, 3.1, 0.85),
            WordItem("princess", 3.1, 3.6, 0.93),
            WordItem("in", 4.0, 4.1, 0.89),
            WordItem("a", 4.1, 4.2, 0.86),
            WordItem("far", 4.2, 4.4, 0.90),
            WordItem("away", 4.4, 4.7, 0.88),
            WordItem("land", 4.7, 5.0, 0.92),
        ]

        boundaries = align_page_phrases(
            expected_phrases=phrases, tokens=tokens, page_start_ms=1000, page_end_ms=6000
        )

        assert len(boundaries) == 3, "Should align all three phrases"

        # Verify each phrase is properly aligned
        for i, boundary in enumerate(boundaries):
            assert boundary["phrase_idx"] == i
            assert boundary["start"] is not None
            assert boundary["end"] is not None
            assert boundary["start"] <= boundary["end"]
            assert len(boundary["parsed"]) > 0, f"Phrase {i} should have parsed text"

        # Verify continuity
        for i in range(len(boundaries) - 1):
            assert boundaries[i]["end"] <= boundaries[i + 1]["start"]

    def test_realistic_misalignment_scenarios(self: TestAlignPagePhrases) -> None:
        """Test alignment with realistic misalignment scenarios."""
        scenarios: list[ScenarioDict] = [
            {
                "name": "Missing words in transcript",
                "phrases": ["the quick brown fox", "jumps over the lazy dog"],
                "tokens": [
                    WordItem("quick", 1.0, 1.3, 0.9),
                    WordItem("brown", 1.3, 1.6, 0.85),
                    # "fox" missing
                    WordItem("jumps", 2.0, 2.3, 0.88),
                    WordItem("over", 2.3, 2.6, 0.92),
                    WordItem("lazy", 3.0, 3.3, 0.87),
                    WordItem("dog", 3.3, 3.6, 0.90),
                ],
                "expected_phrases": 2,
            },
            {
                "name": "Extra words in transcript",
                "phrases": ["hello world"],
                "tokens": [
                    WordItem("well", 0.5, 0.8, 0.85),  # Extra word
                    WordItem("hello", 1.0, 1.3, 0.95),
                    WordItem("there", 1.3, 1.6, 0.88),  # Extra word
                    WordItem("world", 1.6, 2.0, 0.92),
                    WordItem("today", 2.0, 2.4, 0.87),  # Extra word
                ],
                "expected_phrases": 1,
            },
        ]

        for scenario in scenarios:
            phrases: list[str] = scenario["phrases"]
            tokens: list[WordItem] = scenario["tokens"]
            expected_phrase_count: int = scenario["expected_phrases"]
            boundaries = align_page_phrases(
                expected_phrases=phrases,
                tokens=tokens,
                page_start_ms=500,
                # Start before first token
                page_end_ms=5000,  # End after last token
            )

            assert len(boundaries) == expected_phrase_count, (
                f"Scenario '{scenario['name']}' failed: expected {expected_phrase_count} "
                f"phrases, got {len(boundaries)}"
            )

            # All boundaries should have valid structure
            for boundary in boundaries:
                assert "phrase_idx" in boundary
                assert "start" in boundary
                assert "end" in boundary
                assert "parsed" in boundary


@pytest.mark.unit
class TestSaveTimingFile:
    """Test cases for save_timing_file function."""

    def test_save_timing_file_basic(self: TestSaveTimingFile, sample_activity: Activity) -> None:
        """Test basic timing file saving functionality."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp_file:
            timing_path = Path(temp_file.name)

        try:
            # Setup activity with aligned phrases
            sample_activity.pages[0].aligned_phrases = [
                {"phrase_idx": 0, "start": 1000, "end": 2000, "parsed": "hello world"},
                {"phrase_idx": 1, "start": 2500, "end": 3500, "parsed": "how are you"},
            ]

            save_timing_file(activity=sample_activity, timing_path=timing_path)

            # Verify file was created and contains expected data
            assert timing_path.exists(), "Timing file should be created"

            with open(timing_path) as f:
                data = json.load(f)

            assert "full_transcript" in data
            assert "pages" in data
            assert len(data["pages"]) == 1

            page_data = data["pages"][0]
            assert page_data["page_index"] == 0
            assert page_data["page_start"] == 1000
            assert page_data["page_end"] == 5000
            assert len(page_data["phrases"]) == 2

            # Check phrase data includes expected text
            phrase = page_data["phrases"][0]
            assert "phrase_text" in phrase
            assert phrase["phrase_idx"] == sample_activity.pages[0].phrase_indices[0]

        finally:
            # Cleanup
            if timing_path.exists():
                timing_path.unlink()

    def test_save_timing_file_no_aligned_phrases(
        self: TestSaveTimingFile, sample_activity: Activity
    ) -> None:
        """Test saving timing file when page has no aligned phrases."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp_file:
            timing_path = Path(temp_file.name)

        try:
            # Clear aligned phrases
            sample_activity.pages[0].aligned_phrases = None

            save_timing_file(activity=sample_activity, timing_path=timing_path)

            with open(timing_path) as f:
                data = json.load(f)

            page_data = data["pages"][0]
            # Should create default phrase entries
            assert len(page_data["phrases"]) == len(sample_activity.pages[0].phrases)

            for phrase in page_data["phrases"]:
                assert phrase["start"] == sample_activity.pages[0].start_time
                assert phrase["end"] == sample_activity.pages[0].start_time
                assert phrase["parsed"] == ""
                assert "phrase_text" in phrase

        finally:
            if timing_path.exists():
                timing_path.unlink()

    def test_save_timing_file_sets_activity_path(self, sample_activity: Activity) -> None:
        """Test that save_timing_file sets the timing_path on activity."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp_file:
            timing_path = Path(temp_file.name)

        try:
            save_timing_file(activity=sample_activity, timing_path=timing_path)

            assert sample_activity.timing_path == str(timing_path)

        finally:
            if timing_path.exists():
                timing_path.unlink()


@pytest.mark.unit
class TestProcessActivityTiming:
    """Test cases for process_activity_timing function."""

    def test_process_activity_timing_success(self, sample_activity: Activity) -> None:
        """Test successful activity timing processing."""
        # Create mock manifest pages
        mock_manifest_pages = [
            Mock(start=1000, end=5000),
        ]

        with patch(
            "src.orf_rescoring_pipeline.alignment.phrase_alignment.align_page_phrases"
        ) as mock_align:
            mock_align.return_value = [
                {"phrase_idx": 0, "start": 1000, "end": 2000, "parsed": "hello world"},
                {"phrase_idx": 1, "start": 2500, "end": 3500, "parsed": "how are you"},
            ]

            with patch(
                "src.orf_rescoring_pipeline.alignment.phrase_alignment.save_timing_file"
            ) as mock_save:
                process_activity_timing(
                    activity=sample_activity, manifest_pages=mock_manifest_pages
                )

                # Should set page timing from manifest
                assert sample_activity.pages[0].start_time == 1000
                assert sample_activity.pages[0].end_time == 5000

                # Should set aligned phrases
                assert sample_activity.pages[0].aligned_phrases is not None
                assert len(sample_activity.pages[0].aligned_phrases) == 2

                # Should call save_timing_file
                mock_save.assert_called_once()

    def test_process_activity_timing_no_transcript(self: TestProcessActivityTiming) -> None:
        """Test processing when activity has no transcript."""
        # Create a fresh activity without transcript for this test
        import uuid

        from src.orf_rescoring_pipeline.models import Activity, PageData

        activity = Activity(
            activity_id="test_no_transcript",
            story_id=uuid.uuid4(),
            phrases=["hello world", "how are you"],
            pages=[
                PageData(
                    page_index=0,
                    phrase_indices=[0, 1],
                    phrases=["hello world", "how are you"],
                )
            ],
            transcript_json=None,  # No transcript
        )

        mock_manifest_pages = [Mock(start=1000, end=5000)]

        # Should return early without processing
        with pytest.raises(ValueError, match="No transcript available"):
            process_activity_timing(activity=activity, manifest_pages=mock_manifest_pages)

    def test_process_activity_timing_no_pages(self, sample_activity: Activity) -> None:
        """Test processing when activity has no pages."""
        sample_activity.pages = []
        mock_manifest_pages = [Mock(start=1000, end=5000)]

        # Should return early without processing
        with pytest.raises(ValueError, match="No page data available"):
            process_activity_timing(activity=sample_activity, manifest_pages=mock_manifest_pages)

    def test_process_activity_timing_more_pages_than_audio(self, sample_activity: Activity) -> None:
        """Test processing when story has more pages than available audio."""
        # Add extra page to activity
        extra_page = PageData(
            page_index=1,
            phrase_indices=[3, 4],
            phrases=["extra phrase", "another phrase"],
        )
        sample_activity.pages.append(extra_page)

        # Only one manifest page available
        mock_manifest_pages = [Mock(start=1000, end=5000)]

        with patch(
            "src.orf_rescoring_pipeline.alignment.phrase_alignment.align_page_phrases"
        ) as mock_align:
            mock_align.return_value = []

            with patch("src.orf_rescoring_pipeline.alignment.phrase_alignment.save_timing_file"):
                process_activity_timing(
                    activity=sample_activity, manifest_pages=mock_manifest_pages
                )

                # First page should be processed
                assert sample_activity.pages[0].start_time == 1000
                assert sample_activity.pages[0].end_time == 5000

                # Second page should have empty aligned_phrases
                assert sample_activity.pages[1].aligned_phrases == []

    def test_process_activity_timing_save_files_false(self, sample_activity: Activity) -> None:
        """Test processing with save_files=False."""
        mock_manifest_pages = [Mock(start=1000, end=5000)]

        with patch(
            "src.orf_rescoring_pipeline.alignment.phrase_alignment.align_page_phrases"
        ) as mock_align:
            mock_align.return_value = []

            with patch(
                "src.orf_rescoring_pipeline.alignment.phrase_alignment.save_timing_file"
            ) as mock_save:
                process_activity_timing(
                    activity=sample_activity, manifest_pages=mock_manifest_pages, save_files=False
                )

                # Should not call save_timing_file
                mock_save.assert_not_called()

    @patch("src.orf_rescoring_pipeline.alignment.phrase_alignment.align_page_phrases")
    def test_process_activity_timing_alignment_exception(
        self, mock_align: Mock, sample_activity: Activity
    ) -> None:
        """Test processing when alignment raises exception."""
        mock_manifest_pages = [Mock(start=1000, end=5000)]
        mock_align.side_effect = Exception("Alignment failed")

        with pytest.raises(Exception, match="Alignment failed"):
            process_activity_timing(activity=sample_activity, manifest_pages=mock_manifest_pages)
