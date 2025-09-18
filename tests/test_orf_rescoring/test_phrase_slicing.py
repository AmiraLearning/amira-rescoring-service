"""
Test suite for phrase slicing functionality (audio processing).

This module tests the audio processing functions that load audio data from S3
and slice it into phrase segments for ASR processing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.orf_rescoring_pipeline.alignment.audio_processing import (
    load_activity_audio_data_from_s3,
    slice_audio_file_data,
)
from src.orf_rescoring_pipeline.models import Activity, PageData


@pytest.mark.unit
class TestLoadActivityAudioDataFromS3:
    """Test cases for load_activity_audio_data_from_s3 function."""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.get_client")
    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.s3_addr_from_uri")
    async def test_successful_audio_loading(
        self: TestLoadActivityAudioDataFromS3,
        mock_s3_addr: Mock,
        mock_get_client: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test successful loading of audio data from S3."""
        # Setup mocks
        mock_s3_addr.return_value = Mock(bucket="test-bucket", key="test_activity_123/complete.wav")
        mock_s3_client = Mock()
        mock_get_client.return_value = mock_s3_client

        # Mock the download_file method to write audio data to temp file
        def mock_download_file(bucket: str, key: str, filename: str) -> None:
            with open(filename, "wb") as f:
                f.write(mock_audio_data)

        mock_s3_client.download_file.side_effect = mock_download_file

        # Test the function
        await load_activity_audio_data_from_s3(activity=sample_activity, audio_bucket="test-bucket")

        # Verify results
        assert sample_activity.audio_file_data is not None
        assert sample_activity.audio_file_data == mock_audio_data

        # Verify S3 operations were called correctly
        mock_s3_addr.assert_called_once_with("s3://test-bucket/test_activity_123/complete.wav")
        mock_s3_client.download_file.assert_called_once()

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.get_client")
    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.s3_addr_from_uri")
    async def test_s3_download_failure(
        self: TestLoadActivityAudioDataFromS3,
        mock_s3_addr: Mock,
        mock_get_client: Mock,
        sample_activity: Activity,
    ) -> None:
        """Test handling of S3 download failure."""
        # Setup mocks
        mock_s3_addr.return_value = Mock(bucket="test-bucket", key="test_activity_123/complete.wav")
        mock_s3_client = Mock()
        mock_get_client.return_value = mock_s3_client

        # Mock download failure
        mock_s3_client.download_file.side_effect = Exception("S3 download failed")

        # Test the function - should raise exception
        with pytest.raises(Exception, match="S3 download failed"):
            await load_activity_audio_data_from_s3(
                activity=sample_activity, audio_bucket="test-bucket"
            )

        # Verify audio_file_data is set to None
        assert sample_activity.audio_file_data is None

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.get_client")
    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.s3_addr_from_uri")
    @patch("builtins.open")
    async def test_file_read_failure(
        self: TestLoadActivityAudioDataFromS3,
        mock_open: Mock,
        mock_s3_addr: Mock,
        mock_get_client: Mock,
        sample_activity: Activity,
    ) -> None:
        """Test handling of file read failure after successful download."""
        # Setup mocks
        mock_s3_addr.return_value = Mock(bucket="test-bucket", key="test_activity_123/complete.wav")
        mock_s3_client = Mock()
        mock_get_client.return_value = mock_s3_client

        # Mock successful download
        mock_s3_client.download_file.return_value = None

        # Mock file read failure
        mock_open.side_effect = OSError("File read failed")

        # Test the function - should raise exception due to file read failure
        with pytest.raises(OSError, match="File read failed"):
            await load_activity_audio_data_from_s3(
                activity=sample_activity, audio_bucket="test-bucket"
            )

    async def test_correct_s3_uri_construction(
        self: TestLoadActivityAudioDataFromS3, sample_activity: Activity
    ) -> None:
        """Test that S3 URI is constructed correctly."""
        with patch(
            "src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.s3_addr_from_uri"
        ) as mock_s3_addr:
            with patch(
                "src.orf_rescoring_pipeline.alignment.audio_processing.s3_utils.get_client"
            ) as mock_get_client:
                mock_s3_client = Mock()
                mock_get_client.return_value = mock_s3_client
                mock_s3_client.download_file.side_effect = Exception("Test exception")

                try:
                    await load_activity_audio_data_from_s3(
                        activity=sample_activity, audio_bucket="my-audio-bucket"
                    )
                except Exception:
                    pass  # We expect an exception

                # Verify correct URI was constructed
                expected_uri = f"s3://my-audio-bucket/{sample_activity.activity_id}/complete.wav"
                mock_s3_addr.assert_called_once_with(expected_uri)


@pytest.mark.unit
class TestSliceAudioFileData:
    """Test cases for slice_audio_file_data function."""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_successful_audio_slicing(
        self: TestSliceAudioFileData,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test successful slicing of audio data."""
        # Setup activity with audio data and aligned phrases
        sample_activity.audio_file_data = mock_audio_data
        sample_activity.pages[0].aligned_phrases = [
            {"start": 1000, "end": 2000, "parsed": "hello world"},
            {"start": 2500, "end": 3500, "parsed": "how are you"},
        ]

        # Mock AudioSegment using MagicMock to handle __getitem__
        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio

        # Mock audio slicing
        mock_sliced_audio = Mock()
        mock_audio.__getitem__.return_value = mock_sliced_audio

        # Mock export
        mock_sliced_audio.export.return_value = None

        # Test slicing phrase at index 0 (first phrase in page)
        phrase_index = sample_activity.pages[0].phrase_indices[0]
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        # Verify audio slicing was called with correct times
        mock_audio.__getitem__.assert_called_once_with(slice(1000, 2000))

        # Verify timing information
        assert start_ms == 1000
        assert end_ms == 2000

        # Verify export was called
        mock_sliced_audio.export.assert_called_once()

    def test_no_audio_file_data(self: TestSliceAudioFileData, sample_activity: Activity) -> None:
        """Test slicing when activity has no audio file data."""
        sample_activity.audio_file_data = None

        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=0
        )

        assert audio_data is None
        assert start_ms is None
        assert end_ms is None

    def test_phrase_not_in_any_page(
        self: TestSliceAudioFileData, sample_activity: Activity, mock_audio_data: bytes
    ) -> None:
        """Test slicing when phrase index is not found in any page."""
        sample_activity.audio_file_data = mock_audio_data

        # Try to slice a phrase that doesn't exist in any page
        non_existent_phrase_index = 999

        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=non_existent_phrase_index
        )

        assert audio_data is None
        assert start_ms is None
        assert end_ms is None

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_phrase_not_aligned(
        self: TestSliceAudioFileData,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test slicing when phrase exists but is not aligned."""
        sample_activity.audio_file_data = mock_audio_data

        # Page has phrase but no aligned_phrases
        sample_activity.pages[0].aligned_phrases = []

        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio

        phrase_index = sample_activity.pages[0].phrase_indices[0]
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        assert audio_data is None
        # start_ms and end_ms should still be None since phrase wasn't found in aligned_phrases
        assert start_ms is None
        assert end_ms is None

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_aligned_phrases_index_error(
        self: TestSliceAudioFileData,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test handling of IndexError when accessing aligned phrases."""
        sample_activity.audio_file_data = mock_audio_data

        # Set up scenario where phrase is in page but aligned_phrases is shorter
        sample_activity.pages[0].phrase_indices = [0, 1, 2]  # 3 phrases
        sample_activity.pages[0].aligned_phrases = [
            {"start": 1000, "end": 2000, "parsed": "hello world"}
        ]  # Only 1 aligned phrase

        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio

        # Try to access phrase at index 1 (second phrase), but only first is aligned
        phrase_index = 1  # This will be relative index 1 in the page
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        assert audio_data is None

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_audio_export_returns_bytes(
        self: TestSliceAudioFileData,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test that audio export returns proper bytes data."""
        sample_activity.audio_file_data = mock_audio_data
        sample_activity.pages[0].aligned_phrases = [
            {"start": 1000, "end": 2000, "parsed": "hello world"},
        ]

        # Mock AudioSegment and slicing
        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio

        mock_sliced_audio = Mock()
        mock_audio.__getitem__.return_value = mock_sliced_audio

        # Mock export to write to buffer
        def mock_export(buffer: Any, format: str) -> None:
            buffer.write(b"fake_audio_data")

        mock_sliced_audio.export.side_effect = mock_export

        phrase_index = sample_activity.pages[0].phrase_indices[0]
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        assert audio_data == b"fake_audio_data"
        assert start_ms == 1000
        assert end_ms == 2000

    def test_multiple_pages_phrase_lookup(
        self: TestSliceAudioFileData, sample_activity: Activity, mock_audio_data: bytes
    ) -> None:
        """Test phrase lookup across multiple pages."""
        sample_activity.audio_file_data = mock_audio_data

        # Add second page
        page2 = PageData(
            page_index=1,
            phrase_indices=[3, 4],  # Different phrase indices
            phrases=["good morning", "nice day"],
            start_time=5000,
            end_time=8000,
            aligned_phrases=[
                {"start": 5000, "end": 6000, "parsed": "good morning"},
                {"start": 6500, "end": 7500, "parsed": "nice day"},
            ],
        )
        sample_activity.pages.append(page2)

        with patch(
            "src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file"
        ) as mock_from_file:
            mock_audio = MagicMock()
            mock_from_file.return_value = mock_audio

            mock_sliced_audio = Mock()
            mock_audio.__getitem__.return_value = mock_sliced_audio
            mock_sliced_audio.export.return_value = None

            # Look for phrase index 4 (should be found in second page)
            phrase_index = 4
            audio_data, (start_ms, end_ms) = slice_audio_file_data(
                activity=sample_activity, phrase_index=phrase_index
            )

            # Should slice with timing from second page
            mock_audio.__getitem__.assert_called_once_with(slice(6500, 7500))
            assert start_ms == 6500
            assert end_ms == 7500


@pytest.mark.unit
class TestAudioProcessingEdgeCases:
    """Test edge cases and error conditions for audio processing."""

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_zero_duration_slice(
        self: TestAudioProcessingEdgeCases,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test slicing when start and end times are the same."""
        sample_activity.audio_file_data = mock_audio_data
        sample_activity.pages[0].aligned_phrases = [
            {"start": 1000, "end": 1000, "parsed": ""},  # Zero duration
        ]

        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio
        mock_sliced_audio = Mock()
        mock_audio.__getitem__.return_value = mock_sliced_audio
        mock_sliced_audio.export.return_value = None

        phrase_index = sample_activity.pages[0].phrase_indices[0]
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        # Should still attempt to slice, even with zero duration
        mock_audio.__getitem__.assert_called_once_with(slice(1000, 1000))
        assert start_ms == 1000
        assert end_ms == 1000

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_negative_timing(
        self: TestAudioProcessingEdgeCases,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test handling of negative timing values."""
        sample_activity.audio_file_data = mock_audio_data
        sample_activity.pages[0].aligned_phrases = [
            {"start": -100, "end": 1000, "parsed": "hello world"},
        ]

        mock_audio = MagicMock()
        mock_from_file.return_value = mock_audio
        mock_sliced_audio = Mock()
        mock_audio.__getitem__.return_value = mock_sliced_audio
        mock_sliced_audio.export.return_value = None

        phrase_index = sample_activity.pages[0].phrase_indices[0]
        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=phrase_index
        )

        # Should use the negative timing as-is (AudioSegment should handle it)
        mock_audio.__getitem__.assert_called_once_with(slice(-100, 1000))
        assert start_ms == -100
        assert end_ms == 1000

    @patch("src.orf_rescoring_pipeline.alignment.audio_processing.AudioSegment.from_file")
    def test_audio_segment_creation_failure(
        self: TestAudioProcessingEdgeCases,
        mock_from_file: Mock,
        sample_activity: Activity,
        mock_audio_data: bytes,
    ) -> None:
        """Test handling of AudioSegment creation failure."""
        sample_activity.audio_file_data = mock_audio_data

        # Mock AudioSegment.from_file to raise exception
        mock_from_file.side_effect = Exception("Invalid audio data")

        # Function should handle exception gracefully and return None
        result = slice_audio_file_data(activity=sample_activity, phrase_index=0)

        # Should return None for audio data and timing when exception occurs
        audio_data, (start_ms, end_ms) = result
        assert audio_data is None
        assert start_ms is None
        assert end_ms is None

    def test_empty_pages_list(
        self: TestAudioProcessingEdgeCases, sample_activity: Activity, mock_audio_data: bytes
    ) -> None:
        """Test slicing when activity has no pages."""
        sample_activity.audio_file_data = mock_audio_data
        sample_activity.pages = []

        audio_data, (start_ms, end_ms) = slice_audio_file_data(
            activity=sample_activity, phrase_index=0
        )

        assert audio_data is None
        assert start_ms is None
        assert end_ms is None
