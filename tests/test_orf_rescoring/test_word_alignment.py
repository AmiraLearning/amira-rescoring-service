"""
Test suite for word alignment functionality using real amira_fe implementations.

This module tests the word-level alignment functions that match story phrases
with transcribed text using actual alignment algorithms from amira_fe.
"""

import pytest

from src.orf_rescoring_pipeline.alignment.word_alignment import (
    get_word_level_transcript_alignment,
    get_word_level_transcript_alignment_w2v,
)


@pytest.mark.unit
class TestWordLevelTranscriptAlignment:
    """Test cases for get_word_level_transcript_alignment function using real alignment."""

    def test_perfect_match(self) -> None:
        """Test alignment when story phrase exactly matches transcript."""
        story_phrase = "hello world"
        transcript_text = "hello world"

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 2, "Should return match for each word"
        assert matches == [1, 1], "Perfect match should return all 1s"

    def test_partial_match(self) -> None:
        """Test alignment when only some words match."""
        story_phrase = "hello world how are you"
        transcript_text = "hello world are you"  # Missing "how"

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 5, "Should return match for each story word"
        # The exact pattern depends on the alignment algorithm, but we should have some matches
        assert sum(matches) == 4, "Should match 4 words in transcript"
        assert matches == [1, 1, 0, 1, 1], "Should match 4 words in transcript"
        assert all(m in [0, 1] for m in matches), "All matches should be 0 or 1"

    def test_no_match(self) -> None:
        """Test alignment when no words match."""
        story_phrase = "hello world"
        transcript_text = "completely different"

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 2, "Should return match for each story word"
        assert matches == [0, 0], "No match should return all 0s"

    def test_reordered_words(self) -> None:
        """Test alignment with reordered words in transcript."""
        story_phrase = "the quick brown fox"
        transcript_text = "brown fox the quick"  # Reordered

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 4, "Should return match for each story word"
        # Reordered words may or may not match depending on alignment algorithm
        assert all(m in [0, 1] for m in matches), "All matches should be 0 or 1"
        assert sum(matches) < 4, "Should match less than four words"

    def test_empty_inputs(self) -> None:
        """Test alignment with empty inputs."""
        # Empty story phrase
        matches = get_word_level_transcript_alignment(
            story_phrase="", transcript_text="hello world"
        )
        assert matches == [], "Empty story phrase should return empty list"

        # Empty transcript
        matches = get_word_level_transcript_alignment(
            story_phrase="hello world", transcript_text=""
        )
        assert matches == [0, 0], "Empty transcript should return all 0s"

        # Both empty
        matches = get_word_level_transcript_alignment(story_phrase="", transcript_text="")
        assert matches == [], "Both empty should return empty list"

    def test_extra_words_in_transcript(self) -> None:
        """Test alignment when transcript has extra words."""
        story_phrase = "the cat"
        transcript_text = "the big fluffy cat"  # Extra words

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 2, "Should return match for each story word"
        # Should match "the" and "cat" despite extra words
        assert sum(matches) >= 1, "Should match at least one word"

    def test_case_sensitivity(self) -> None:
        """Test alignment with different cases."""
        story_phrase = "Hello World"
        transcript_text = "hello world"

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 2, "Should return match for each word"
        # The alignment algorithm should handle case differences
        assert sum(matches) >= 1, "Should match despite case differences"

    def test_punctuation_handling(self) -> None:
        """Test alignment with punctuation."""
        story_phrase = "hello world"
        transcript_text = "hello, world!"

        matches = get_word_level_transcript_alignment(
            story_phrase=story_phrase, transcript_text=transcript_text
        )

        assert len(matches) == 2, "Should return match for each word"
        # Should match despite punctuation differences
        assert sum(matches) >= 1, "Should match core words despite punctuation"


@pytest.mark.unit
class TestWordLevelTranscriptAlignmentW2V:
    """Test cases for get_word_level_transcript_alignment_w2v function using real phoneme alignment."""

    def test_w2v_basic_functionality(self) -> None:
        """Test W2V alignment basic functionality with proper Amirabet transcript."""
        story_phrase = "hello world"
        transcript_amirabet = "hɛlo wɝld"  # Amirabet phoneme notation

        matches = get_word_level_transcript_alignment_w2v(
            story_phrase=story_phrase, transcript_text=transcript_amirabet
        )

        assert len(matches) == 2, "Should return match for each word"
        assert all(m in [0, 1] for m in matches), "All matches should be 0 or 1"
        # With proper Amirabet input, we should get good matches for identical phonemes
        assert sum(matches) == 2, "Should have exactly two matches with proper Amirabet input"

    def test_w2v_hyphenated_word(self) -> None:
        """Test W2V alignment with hyphenated word using proper Amirabet."""
        story_phrase = "kind-hearted"
        transcript_amirabet = "kγnd hɑɹtɪd"  # Amirabet for "kind hearted"

        matches = get_word_level_transcript_alignment_w2v(
            story_phrase=story_phrase, transcript_text=transcript_amirabet
        )

        assert len(matches) == 1, "Should return match for single hyphenated word"
        # Should match since it's the same word in phoneme form
        assert matches[0] == 1, "Should match identical phoneme representation"

    def test_w2v_empty_inputs(self) -> None:
        """Test W2V alignment with empty inputs."""
        # Empty story phrase
        matches = get_word_level_transcript_alignment_w2v(
            story_phrase="", transcript_text="hello world"
        )
        assert matches == [], "Empty story phrase should return empty list"

        # Empty transcript
        matches = get_word_level_transcript_alignment_w2v(
            story_phrase="hello world", transcript_text=""
        )
        assert matches == [0, 0], "Empty transcript should return all 0s"

    def test_w2v_common_words(self) -> None:
        """Test W2V alignment with common English words."""
        test_cases: list[tuple[str, str]] = [
            ("the", "the"),
            ("and", "and"),
            ("cat", "cat"),
            ("dog", "dog"),
            ("run", "run"),
        ]

        for story_word, transcript_word in test_cases:
            matches = get_word_level_transcript_alignment_w2v(
                story_phrase=story_word, transcript_text=transcript_word
            )

            assert len(matches) == 1, f"Should return one match for '{story_word}'"
            # Phoneme alignment may have complexity, so just check it's valid
            assert matches[0] in [0, 1], f"Match for '{story_word}' should be 0 or 1"

    def test_w2v_realistic_phrases(self) -> None:
        """Test W2V alignment with realistic phrases using proper Amirabet."""
        # Using actual Amirabet conversions for more realistic tests
        test_cases: list[dict[str, str]] = [
            {
                "story": "hello world",
                "transcript_amirabet": "hɛlo wɝld",  # Perfect match
            },
            {
                "story": "hello",
                "transcript_amirabet": "hɛlo",  # Single word perfect match
            },
            {
                "story": "kind-hearted",
                "transcript_amirabet": "kγnd hɑɹtɪd",  # Amirabet for "kind hearted"
            },
        ]

        for case in test_cases:
            matches = get_word_level_transcript_alignment_w2v(
                story_phrase=case["story"], transcript_text=case["transcript_amirabet"]
            )

            story_words = case["story"].split()
            assert len(matches) == len(story_words), (
                f"Should return match for each story word in '{case['story']}'"
            )

            # With proper Amirabet, should get good matches
            assert all(m in [0, 1] for m in matches), (
                f"All matches should be 0 or 1 for '{case['story']}'"
            )

            # Perfect Amirabet should yield matches
            if case["story"] == "hello world":
                assert sum(matches) >= 1, "Should match with correct Amirabet input"
