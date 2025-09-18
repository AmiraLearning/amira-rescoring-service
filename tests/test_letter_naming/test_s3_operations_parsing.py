"""Test coverage for S3 operations parsing edge cases."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from infra.s3_client import HighPerformanceS3Config, ProductionS3Client, S3OperationResult
from utils.s3_audio_operations import s3_find


class TestS3OperationsParsing:
    """Test S3 operations parsing edge cases and robustness."""

    @pytest.mark.asyncio
    async def test_s3_find_empty_results(self) -> None:
        """Test s3_find handles empty results gracefully."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        mock_result.data = {"objects": []}
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="test-prefix/",
            s3_client=mock_client,
            retry_limit=1,
        )

        assert result == []
        mock_client.list_objects_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_s3_find_malformed_objects_parsing(self) -> None:
        """Test s3_find parsing handles malformed object data."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        # Mix of valid and malformed object data
        mock_result.data = {
            "objects": [
                {"Key": "valid-key-1.wav", "Size": 1024},  # Valid
                {"Size": 2048},  # Missing Key
                {"Key": None, "Size": 512},  # Null Key
                "not-a-dict",  # Not a dictionary
                {"Key": "valid-key-2.wav"},  # Valid, missing Size (acceptable)
                {"Key": "", "Size": 256},  # Empty key
            ]
        }
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="test-prefix/",
            s3_client=mock_client,
        )

        # Should only return valid keys, filtering out malformed entries
        expected_keys = ["valid-key-1.wav", "valid-key-2.wav"]
        assert result == expected_keys

    @pytest.mark.asyncio
    async def test_s3_find_none_data_handling(self) -> None:
        """Test s3_find handles None data gracefully."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        mock_result.data = None
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="test-prefix/",
            s3_client=mock_client,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_s3_find_missing_objects_key(self) -> None:
        """Test s3_find handles missing 'objects' key in response."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        mock_result.data = {"other_field": "value"}  # Missing 'objects' key
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="test-prefix/",
            s3_client=mock_client,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_s3_find_network_failure_retry(self) -> None:
        """Test s3_find retry logic on network failures."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_failed_result = Mock(spec=S3OperationResult)
        mock_failed_result.success = False
        mock_failed_result.error = "Network timeout"

        mock_success_result = Mock(spec=S3OperationResult)
        mock_success_result.success = True
        mock_success_result.data = {"objects": [{"Key": "retry-success.wav"}]}

        # First call fails, second succeeds
        mock_client.list_objects_batch = AsyncMock(
            side_effect=[[mock_failed_result], [mock_success_result]]
        )

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="test-prefix/",
            s3_client=mock_client,
            retry_limit=2,
        )

        assert result == ["retry-success.wav"]
        assert mock_client.list_objects_batch.call_count == 2

    @pytest.mark.asyncio
    async def test_s3_find_all_retries_exhausted(self) -> None:
        """Test s3_find when all retries are exhausted."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_failed_result = Mock(spec=S3OperationResult)
        mock_failed_result.success = False
        mock_failed_result.error = "Persistent network error"
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_failed_result])

        with pytest.raises(RuntimeError, match="S3 list failed"):
            await s3_find(
                source_bucket="test-bucket",
                prefix_path="test-prefix/",
                s3_client=mock_client,
                retry_limit=3,
            )

        assert mock_client.list_objects_batch.call_count == 3

    @pytest.mark.asyncio
    async def test_s3_find_unicode_keys_handling(self) -> None:
        """Test s3_find handles Unicode and special characters in keys."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        mock_result.data = {
            "objects": [
                {"Key": "普通话-test.wav"},  # Chinese characters
                {"Key": "file with spaces.mp3"},  # Spaces
                {"Key": "special-chars_@#$.wav"},  # Special characters
                {"Key": "emoji-😀.wav"},  # Emoji
                {"Key": "نسبة.wav"},  # Arabic
            ]
        }
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="unicode-test/",
            s3_client=mock_client,
        )

        # All Unicode keys should be preserved as strings
        expected_keys = [
            "普通话-test.wav",
            "file with spaces.mp3",
            "special-chars_@#$.wav",
            "emoji-😀.wav",
            "نسبة.wav",
        ]
        assert result == expected_keys

    @pytest.mark.asyncio
    async def test_s3_find_numeric_keys_conversion(self) -> None:
        """Test s3_find properly converts non-string keys to strings."""
        mock_client = Mock(spec=ProductionS3Client)
        mock_result = Mock(spec=S3OperationResult)
        mock_result.success = True
        mock_result.data = {
            "objects": [
                {"Key": 12345},  # Integer key
                {"Key": 67.89},  # Float key
                {"Key": True},  # Boolean key
                {"Key": "normal-key.wav"},  # Normal string key
            ]
        }
        mock_client.list_objects_batch = AsyncMock(return_value=[mock_result])

        result = await s3_find(
            source_bucket="test-bucket",
            prefix_path="mixed-types/",
            s3_client=mock_client,
        )

        # All keys should be converted to strings
        expected_keys = ["12345", "67.89", "True", "normal-key.wav"]
        assert result == expected_keys


class TestS3ClientMemoryBackpressure:
    """Test S3 client memory backpressure functionality."""

    def test_s3_config_adaptive_buffer_sizing_defaults(self) -> None:
        """Test S3 config has proper defaults for adaptive buffer sizing."""
        config = HighPerformanceS3Config()

        assert hasattr(config, "adaptive_buffer_sizing")
        assert config.adaptive_buffer_sizing is True
        assert hasattr(config, "max_in_flight_bytes")
        assert config.max_in_flight_bytes > 0
        assert hasattr(config, "max_memory_usage_percent")
        assert 50.0 <= config.max_memory_usage_percent <= 95.0

    @pytest.mark.asyncio
    async def test_s3_client_memory_pressure_tracking(self) -> None:
        """Test S3 client tracks memory pressure correctly."""
        config = HighPerformanceS3Config()
        client = ProductionS3Client(config=config)

        # Test initial state
        assert client._in_flight_bytes == 0
        assert client._memory_pressure_factor == 1.0

        # Test reservation logic
        can_reserve = await client._reserve_in_flight_bytes(bytes_count=1000)
        assert can_reserve is True
        assert client._in_flight_bytes == 1000

        # Test release logic
        await client._release_in_flight_bytes(bytes_count=500)
        assert client._in_flight_bytes == 500

        # Test over-limit reservation
        large_bytes = config.max_in_flight_bytes + 1
        can_reserve_large = await client._reserve_in_flight_bytes(bytes_count=large_bytes)
        assert can_reserve_large is False

        await client.close()

    def test_s3_client_adaptive_buffer_size_calculation(self) -> None:
        """Test adaptive buffer size calculation under different memory conditions."""
        config = HighPerformanceS3Config(buffer_size=8192)
        client = ProductionS3Client(config=config)

        # Mock memory conditions and test adaptive sizing
        with patch("psutil.virtual_memory") as mock_memory:
            # Low memory pressure
            mock_memory.return_value.percent = 60.0
            buffer_size = client._get_adaptive_buffer_size()
            assert buffer_size == config.buffer_size  # Full size

            # High memory pressure
            mock_memory.return_value.percent = 90.0
            buffer_size = client._get_adaptive_buffer_size()
            assert buffer_size < config.buffer_size  # Reduced size

        # Test with adaptive sizing disabled
        config.adaptive_buffer_sizing = False
        buffer_size = client._get_adaptive_buffer_size()
        assert buffer_size == config.buffer_size
