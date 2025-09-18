"""
Test suite for rescoring functionality.

This module tests the phrase rescoring functions that transcribe phrases,
align transcripts, and rescore phrase errors based on multiple ASR system outputs.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.orf_rescoring_pipeline.models import Activity, ModelFeature
from src.orf_rescoring_pipeline.rules.rescoring import (
    align_phrase_transcripts,
    rescore_phrase,
    rescore_phrase_errors,
    transcribe_phrase,
)


@pytest.mark.unit
class TestTranscribePhrase:
    """Test cases for transcribe_phrase function."""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_successful_transcription(
        self: "TestTranscribePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test successful phrase transcription with both ASR systems."""
        # Setup mock audio slicing
        mock_audio_data = b"fake_audio_data"
        mock_slice_audio.return_value = (mock_audio_data, (1000, 2000))

        # Setup mock ASR results - sync wrapper handles async clients internally
        from src.orf_rescoring_pipeline.models import TranscriptItem

        mock_kaldi_result = TranscriptItem(transcript="hello world test", words=[])
        mock_w2v_result = TranscriptItem(transcript="hello world test", words=[])

        # Mock ASR clients - the sync wrapper will handle the async calls internally
        mock_kaldi_client.transcribe.return_value = mock_kaldi_result
        mock_w2v_client.transcribe.return_value = mock_w2v_result

        # Test transcription - back to sync call
        kaldi_transcript, w2v_transcript = transcribe_phrase(
            activity=sample_activity, phrase_index=0, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        assert kaldi_transcript == "hello world test"
        assert w2v_transcript == "hello world test"

        # Verify both ASR clients were called
        mock_kaldi_client.transcribe.assert_called_once_with(sample_activity, 0, mock_audio_data)
        mock_w2v_client.transcribe.assert_called_once_with(sample_activity, 0, mock_audio_data)

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_no_audio_data(
        self: "TestTranscribePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test transcription when no audio data is available."""
        # Mock no audio data
        mock_slice_audio.return_value = (None, (None, None))

        kaldi_transcript, w2v_transcript = transcribe_phrase(
            activity=sample_activity, phrase_index=0, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        assert kaldi_transcript == ""
        assert w2v_transcript == ""

        # ASR clients should not be called
        mock_kaldi_client.transcribe.assert_not_called()
        mock_w2v_client.transcribe.assert_not_called()

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_zero_duration_audio(
        self: "TestTranscribePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test transcription when audio has zero duration."""
        # Mock zero duration audio
        mock_audio_data = b"fake_audio_data"
        mock_slice_audio.return_value = (
            mock_audio_data,
            (1000, 1000),
        )  # Same start and end

        kaldi_transcript, w2v_transcript = transcribe_phrase(
            activity=sample_activity, phrase_index=0, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        assert kaldi_transcript == ""
        assert w2v_transcript == ""

        # ASR clients should not be called for zero duration
        mock_kaldi_client.transcribe.assert_not_called()
        mock_w2v_client.transcribe.assert_not_called()

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_parallel_execution(
        self: "TestTranscribePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test that ASR transcriptions are executed in parallel using asyncio.gather."""
        # Setup mock audio slicing
        mock_audio_data = b"fake_audio_data"
        mock_slice_audio.return_value = (mock_audio_data, (1000, 2000))

        # Setup mock ASR results
        from src.orf_rescoring_pipeline.models import TranscriptItem

        mock_kaldi_result = TranscriptItem(transcript="kaldi result", words=[])
        mock_w2v_result = TranscriptItem(transcript="w2v result", words=[])

        mock_kaldi_client.transcribe.return_value = mock_kaldi_result
        mock_w2v_client.transcribe.return_value = mock_w2v_result

        # Test transcription
        kaldi_transcript, w2v_transcript = transcribe_phrase(
            activity=sample_activity, phrase_index=0, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        assert kaldi_transcript == "kaldi result"
        assert w2v_transcript == "w2v result"

        # Verify both ASR clients were called (parallel execution via asyncio.gather)
        mock_kaldi_client.transcribe.assert_called_once_with(sample_activity, 0, mock_audio_data)
        mock_w2v_client.transcribe.assert_called_once_with(sample_activity, 0, mock_audio_data)

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_asr_client_exceptions(
        self: "TestTranscribePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test handling of ASR client exceptions."""
        # Setup mock audio slicing
        mock_audio_data = b"fake_audio_data"
        mock_slice_audio.return_value = (mock_audio_data, (1000, 2000))

        # Setup one client to raise exception
        from src.orf_rescoring_pipeline.models import TranscriptItem

        mock_kaldi_client.transcribe.side_effect = Exception("Kaldi failed")
        mock_w2v_result = TranscriptItem(transcript="w2v success", words=[])
        mock_w2v_client.transcribe.return_value = mock_w2v_result

        # With return_exceptions=True, exceptions are caught and returned as empty strings
        kaldi_transcript, w2v_transcript = transcribe_phrase(
            activity=sample_activity, phrase_index=0, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        # Kaldi should return empty string due to exception, W2V should succeed
        assert kaldi_transcript == ""
        assert w2v_transcript == "w2v success"


@pytest.mark.unit
class TestAlignPhraseTranscripts:
    """Test cases for align_phrase_transcripts function."""

    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment_w2v")
    def test_successful_alignment(
        self: "TestAlignPhraseTranscripts", mock_w2v_align: Mock, mock_kaldi_align: Mock
    ) -> None:
        """Test successful alignment of both transcripts."""
        # Setup mock alignment results
        mock_kaldi_align.return_value = [1, 1, 0, 1]
        mock_w2v_align.return_value = [1, 0, 1, 1]

        story_phrase = "hello world test phrase"
        kaldi_transcript = "hello world phrase"
        w2v_transcript = "hello test phrase"

        kaldi_matches, w2v_matches = align_phrase_transcripts(
            story_phrase=story_phrase,
            kaldi_transcript=kaldi_transcript,
            w2v_transcript=w2v_transcript,
        )

        assert kaldi_matches == [1, 1, 0, 1]
        assert w2v_matches == [1, 0, 1, 1]

        # Verify alignment functions were called correctly
        mock_kaldi_align.assert_called_once_with(
            story_phrase=story_phrase, transcript_text=kaldi_transcript
        )
        mock_w2v_align.assert_called_once_with(
            story_phrase=story_phrase, transcript_text=w2v_transcript
        )

    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment_w2v")
    def test_empty_transcripts(
        self: "TestAlignPhraseTranscripts", mock_w2v_align: Mock, mock_kaldi_align: Mock
    ) -> None:
        """Test alignment with empty transcripts."""
        mock_kaldi_align.return_value = [0, 0, 0]
        mock_w2v_align.return_value = [0, 0, 0]

        story_phrase = "hello world test"
        kaldi_transcript = ""
        w2v_transcript = ""

        kaldi_matches, w2v_matches = align_phrase_transcripts(
            story_phrase=story_phrase,
            kaldi_transcript=kaldi_transcript,
            w2v_transcript=w2v_transcript,
        )

        assert kaldi_matches == [0, 0, 0]
        assert w2v_matches == [0, 0, 0]

    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.get_word_level_transcript_alignment_w2v")
    def test_alignment_exceptions(
        self: "TestAlignPhraseTranscripts", mock_w2v_align: Mock, mock_kaldi_align: Mock
    ) -> None:
        """Test handling of alignment exceptions."""
        # Setup one alignment to fail
        mock_kaldi_align.side_effect = Exception("Kaldi alignment failed")
        mock_w2v_align.return_value = [1, 0, 1]

        story_phrase = "hello world test"
        kaldi_transcript = "hello test"
        w2v_transcript = "hello world"

        # Should propagate the exception
        with pytest.raises(Exception, match="Kaldi alignment failed"):
            align_phrase_transcripts(
                story_phrase=story_phrase,
                kaldi_transcript=kaldi_transcript,
                w2v_transcript=w2v_transcript,
            )


@pytest.mark.unit
class TestRescorePhraseErrors:
    """Test cases for rescore_phrase_errors function."""

    def test_basic_rescoring_logic(self: "TestRescorePhraseErrors") -> None:
        """Test basic error rescoring logic."""
        deepgram_matches = [1, 0, 1, 0, 1]
        kaldi_matches = [0, 1, 1, 0, 0]
        w2v_matches = [1, 0, 0, 1, 1]

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # Expected logic:
        # - If W2V match = 1, then False (not an error)
        # - Elif sum of matches > 1, then False (not an error)
        # - Else True (is an error)

        expected = [
            False,  # w2v_match=1
            False,  # sum([0,1,0])=1, not >1, but w2v=0, so check sum: 1 not >1, so True... wait
            False,  # w2v_match=0, sum([1,1,0])=2 >1, so False
            False,  # w2v_match=1
            False,  # w2v_match=1
        ]

        # Let me recalculate based on the actual logic:
        # Index 0: w2v=1 -> False
        # Index 1: w2v=0, sum([0,1,0])=1 not >1 -> True
        # Index 2: w2v=0, sum([1,1,0])=2 >1 -> False
        # Index 3: w2v=1 -> False
        # Index 4: w2v=1 -> False

        expected = [False, True, False, False, False]
        assert error_array == expected

    def test_w2v_priority(self: "TestRescorePhraseErrors") -> None:
        """Test that W2V matches take priority (always not an error)."""
        deepgram_matches = [0, 0, 0]
        kaldi_matches = [0, 0, 0]
        w2v_matches = [1, 1, 1]  # All W2V matches

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # All should be False (not errors) because W2V matched
        assert error_array == [False, False, False]

    def test_consensus_voting(self: "TestRescorePhraseErrors") -> None:
        """Test consensus voting when W2V doesn't match."""
        deepgram_matches = [1, 1, 0, 0]
        kaldi_matches = [1, 0, 1, 0]
        w2v_matches = [0, 0, 0, 0]  # No W2V matches

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # Expected:
        # Index 0: w2v=0, sum([1,1,0])=2 >1 -> False
        # Index 1: w2v=0, sum([1,0,0])=1 not >1 -> True
        # Index 2: w2v=0, sum([0,1,0])=1 not >1 -> True
        # Index 3: w2v=0, sum([0,0,0])=0 not >1 -> True

        expected = [False, True, True, True]
        assert error_array == expected

    def test_all_systems_agree_correct(self: "TestRescorePhraseErrors") -> None:
        """Test when all systems agree word is correct."""
        deepgram_matches = [1, 1, 1]
        kaldi_matches = [1, 1, 1]
        w2v_matches = [1, 1, 1]

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # All should be False (not errors)
        assert error_array == [False, False, False]

    def test_all_systems_agree_incorrect(self: "TestRescorePhraseErrors") -> None:
        """Test when all systems agree word is incorrect."""
        deepgram_matches = [0, 0, 0]
        kaldi_matches = [0, 0, 0]
        w2v_matches = [0, 0, 0]

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # All should be True (errors)
        assert error_array == [True, True, True]

    def test_empty_matches(self: "TestRescorePhraseErrors") -> None:
        """Test rescoring with empty match arrays."""
        error_array = rescore_phrase_errors(deepgram_matches=[], kaldi_matches=[], w2v_matches=[])

        assert error_array == []

    def test_single_word(self: "TestRescorePhraseErrors") -> None:
        """Test rescoring with single word."""
        deepgram_matches = [1]
        kaldi_matches = [0]
        w2v_matches = [0]

        error_array = rescore_phrase_errors(
            deepgram_matches=deepgram_matches, kaldi_matches=kaldi_matches, w2v_matches=w2v_matches
        )

        # w2v=0, sum([1,0,0])=1 not >1 -> True
        assert error_array == [True]


@pytest.mark.unit
class TestRescorePhrase:
    """Test cases for rescore_phrase function."""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.transcribe_phrase")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.align_phrase_transcripts")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.rescore_phrase_errors")
    def test_successful_rescoring(
        self: "TestRescorePhrase",
        mock_rescore_errors: Mock,
        mock_align: Mock,
        mock_transcribe: Mock,
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test successful phrase rescoring workflow."""
        # Setup mocks
        mock_slice_audio.return_value = (b"audio_data", (1000, 2000))
        mock_transcribe.return_value = ("kaldi transcript", "w2v transcript")
        mock_align.return_value = ([1, 0, 1], [1, 1, 0])
        mock_rescore_errors.return_value = [False, True, False]

        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0, 1],
            kaldi_match=[0, 1, 1],  # Original matches (will be overwritten)
        )

        sample_activity.phrases = ["hello world test"]

        result = rescore_phrase(
            activity=sample_activity, feature=feature, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        (
            error_array,
            kaldi_matches,
            w2v_matches,
            kaldi_transcript,
            w2v_transcript,
        ) = result

        # Verify results
        assert error_array == [False, True, False]
        assert kaldi_matches == [1, 0, 1]
        assert w2v_matches == [1, 1, 0]
        assert kaldi_transcript == "kaldi transcript"
        assert w2v_transcript == "w2v transcript"

        # Verify function call chain
        mock_transcribe.assert_called_once_with(
            activity=sample_activity,
            phrase_index=feature.phrase_index,
            kaldi=mock_kaldi_client,
            w2v=mock_w2v_client,
        )
        mock_align.assert_called_once_with(
            story_phrase="hello world test",
            kaldi_transcript="kaldi transcript",
            w2v_transcript="w2v transcript",
        )
        mock_rescore_errors.assert_called_once_with(
            deepgram_matches=[1, 0, 1], kaldi_matches=[1, 0, 1], w2v_matches=[1, 1, 0]
        )

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_no_audio_data_fallback(
        self: "TestRescorePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test rescoring fallback when no audio data is available."""
        # Mock no audio data
        mock_slice_audio.return_value = (None, (None, None))

        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0, 1],
            kaldi_match=[1, 0, 1, 0, 1],  # 5 elements for fallback
        )

        result = rescore_phrase(
            activity=sample_activity, feature=feature, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        (
            error_array,
            kaldi_matches,
            w2v_matches,
            kaldi_transcript,
            w2v_transcript,
        ) = result

        # Should return fallback values
        assert error_array == [True, True, True, True, True]  # All errors
        assert kaldi_matches == [0, 0, 0, 0, 0]  # All no matches
        assert w2v_matches == [0, 0, 0, 0, 0]  # All no matches
        assert kaldi_transcript == ""
        assert w2v_transcript == ""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    def test_zero_duration_audio_fallback(
        self: "TestRescorePhrase",
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test rescoring fallback when audio has zero duration."""
        # Mock zero duration audio
        mock_slice_audio.return_value = (b"audio_data", (1000, 1000))  # Same start/end

        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0],
            kaldi_match=[1, 1],
        )

        result = rescore_phrase(
            activity=sample_activity, feature=feature, kaldi=mock_kaldi_client, w2v=mock_w2v_client
        )

        (
            error_array,
            kaldi_matches,
            w2v_matches,
            kaldi_transcript,
            w2v_transcript,
        ) = result

        # Should return fallback values
        assert error_array == [True, True]
        assert kaldi_matches == [0, 0]
        assert w2v_matches == [0, 0]
        assert kaldi_transcript == ""
        assert w2v_transcript == ""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.transcribe_phrase")
    def test_transcription_failure_propagation(
        self: "TestRescorePhrase",
        mock_transcribe: Mock,
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test that transcription failures are propagated."""
        mock_slice_audio.return_value = (b"audio_data", (1000, 2000))
        mock_transcribe.side_effect = Exception("Transcription failed")

        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0, 1],
            kaldi_match=[1, 0, 1],
        )

        with pytest.raises(Exception, match="Transcription failed"):
            rescore_phrase(
                activity=sample_activity,
                feature=feature,
                kaldi=mock_kaldi_client,
                w2v=mock_w2v_client,
            )

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.slice_audio_file_data")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.transcribe_phrase")
    @patch("src.orf_rescoring_pipeline.rules.rescoring.align_phrase_transcripts")
    def test_alignment_failure_propagation(
        self: "TestRescorePhrase",
        mock_align: Mock,
        mock_transcribe: Mock,
        mock_slice_audio: Mock,
        sample_activity: Activity,
        mock_kaldi_client: Mock,
        mock_w2v_client: Mock,
    ) -> None:
        """Test that alignment failures are propagated."""
        mock_slice_audio.return_value = (b"audio_data", (1000, 2000))
        mock_transcribe.return_value = ("kaldi", "w2v")
        mock_align.side_effect = Exception("Alignment failed")

        feature = ModelFeature(
            model="test_model",
            phrase_index=0,
            deepgram_match=[1, 0, 1],
            kaldi_match=[1, 0, 1],
        )

        sample_activity.phrases = ["hello world test"]

        with pytest.raises(Exception, match="Alignment failed"):
            rescore_phrase(
                activity=sample_activity,
                feature=feature,
                kaldi=mock_kaldi_client,
                w2v=mock_w2v_client,
            )
