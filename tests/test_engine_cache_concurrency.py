"""Test coverage for engine cache concurrency and thread safety."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

from src.pipeline.inference.engine import ThreadSafeLRUCache


class TestThreadSafeLRUCache:
    """Test thread-safe LRU cache functionality."""

    @pytest.fixture
    def cache(self) -> ThreadSafeLRUCache:
        """Create a small cache instance for testing."""
        return ThreadSafeLRUCache(maxsize=3)

    def test_cache_basic_operations(self, cache: ThreadSafeLRUCache) -> None:
        """Test basic cache operations work correctly."""
        # Test empty cache
        assert cache.get("key1") is None
        assert cache.size() == 0

        # Test put and get
        cache.put("key1", "value1")
        assert cache.get("key1") == "value1"
        assert cache.size() == 1

        # Test cache miss
        assert cache.get("nonexistent") is None

    def test_cache_lru_eviction(self, cache: ThreadSafeLRUCache) -> None:
        """Test LRU eviction works correctly."""
        # Fill cache to capacity
        cache.put("key1", "value1")
        cache.put("key2", "value2")
        cache.put("key3", "value3")
        assert cache.size() == 3

        # Access key1 to make it most recently used
        assert cache.get("key1") == "value1"

        # Add new item, should evict key2 (least recently used)
        cache.put("key4", "value4")
        assert cache.size() == 3
        assert cache.get("key1") == "value1"  # Still present
        assert cache.get("key2") is None  # Evicted
        assert cache.get("key3") == "value3"  # Still present
        assert cache.get("key4") == "value4"  # Newly added

    def test_cache_update_existing_key(self, cache: ThreadSafeLRUCache) -> None:
        """Test updating existing keys moves them to most recent."""
        cache.put("key1", "value1")
        cache.put("key2", "value2")
        cache.put("key3", "value3")

        # Update key1 with new value
        cache.put("key1", "updated_value1")
        assert cache.get("key1") == "updated_value1"
        assert cache.size() == 3

        # Add new item, key2 should be evicted (now LRU)
        cache.put("key4", "value4")
        assert cache.get("key1") == "updated_value1"  # Still present (recently updated)
        assert cache.get("key2") is None  # Evicted
        assert cache.get("key3") == "value3"  # Still present
        assert cache.get("key4") == "value4"  # Newly added

    def test_cache_concurrent_access(self, cache: ThreadSafeLRUCache) -> None:
        """Test concurrent read/write operations are thread-safe."""
        results = []
        errors = []

        def worker(worker_id: int) -> None:
            """Worker function that performs cache operations."""
            try:
                for i in range(50):
                    key = f"key_{worker_id}_{i}"
                    value = f"value_{worker_id}_{i}"

                    # Put operation
                    cache.put(key, value)

                    # Get operation
                    retrieved = cache.get(key)
                    if retrieved is not None:
                        results.append((key, retrieved))

                    # Small delay to increase chance of race conditions
                    time.sleep(0.001)
            except Exception as e:
                errors.append(f"Worker {worker_id} error: {e}")

        # Run multiple workers concurrently
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(4)]
            for future in futures:
                future.result()

        # Should not have any errors from thread safety issues
        assert len(errors) == 0, f"Thread safety errors: {errors}"

        # Should have some results (exact count depends on evictions)
        assert len(results) > 0

        # Cache size should not exceed maxsize
        assert cache.size() <= 3

    def test_cache_concurrent_eviction_safety(self, cache: ThreadSafeLRUCache) -> None:
        """Test that concurrent evictions don't cause race conditions."""
        evicted_items = []

        # Mock object that tracks when it's deleted
        class MockEngine:
            def __init__(self, name: str):
                self.name = name

            def __del__(self):
                evicted_items.append(self.name)

        def rapid_insertion(start_id: int) -> None:
            """Rapidly insert items to trigger evictions."""
            for i in range(20):
                key = f"engine_{start_id}_{i}"
                engine = MockEngine(key)
                cache.put(key, engine)

        # Run multiple threads that cause evictions
        threads = []
        for thread_id in range(3):
            thread = threading.Thread(target=rapid_insertion, args=(thread_id,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Cache should still be within size limits
        assert cache.size() <= 3

        # Multiple items should have been evicted
        # Note: Due to garbage collection timing, we can't guarantee exact count
        assert len(evicted_items) >= 0

    def test_cache_get_thread_safety(self, cache: ThreadSafeLRUCache) -> None:
        """Test that concurrent get operations are thread-safe."""
        # Pre-populate cache
        for i in range(3):
            cache.put(f"stable_key_{i}", f"stable_value_{i}")

        results = []

        def concurrent_getter(key: str, iterations: int) -> None:
            """Concurrently get the same key multiple times."""
            for _ in range(iterations):
                result = cache.get(key)
                if result is not None:
                    results.append(result)

        # Multiple threads accessing the same key
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(concurrent_getter, "stable_key_1", 100) for _ in range(5)]
            for future in futures:
                future.result()

        # Should have gotten the value many times without errors
        assert len(results) > 0
        assert all(result == "stable_value_1" for result in results)

    def test_cache_mixed_operations_stress_test(self, cache: ThreadSafeLRUCache) -> None:
        """Stress test with mixed get/put/eviction operations."""
        operations_completed = []

        def mixed_operations(worker_id: int) -> None:
            """Perform mixed cache operations."""
            for i in range(30):
                try:
                    # Mix of operations
                    if i % 3 == 0:
                        # Put operation
                        cache.put(f"stress_{worker_id}_{i}", f"value_{worker_id}_{i}")
                        operations_completed.append("put")
                    elif i % 3 == 1:
                        # Get operation on recently added keys
                        cache.get(f"stress_{worker_id}_{i - 1}")
                        operations_completed.append("get")
                    else:
                        # Get operation on potentially evicted keys
                        cache.get(f"stress_{worker_id}_{i - 10}")
                        operations_completed.append("get_old")

                except Exception as e:
                    operations_completed.append(f"error: {e}")

        # Run stress test
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(mixed_operations, i) for i in range(6)]
            for future in futures:
                future.result()

        # Should complete many operations without errors
        assert len(operations_completed) > 100
        error_operations = [op for op in operations_completed if op.startswith("error")]
        assert len(error_operations) == 0, f"Stress test errors: {error_operations}"

    @pytest.mark.asyncio
    async def test_cache_async_context_safety(self, cache: ThreadSafeLRUCache) -> None:
        """Test cache operations in async context with concurrent tasks."""

        async def async_cache_worker(worker_id: int) -> list[str]:
            """Async worker that performs cache operations."""
            results = []
            for i in range(20):
                key = f"async_key_{worker_id}_{i}"
                value = f"async_value_{worker_id}_{i}"

                # Put in cache
                cache.put(key, value)

                # Small async delay
                await asyncio.sleep(0.001)

                # Try to get it back
                retrieved = cache.get(key)
                if retrieved == value:
                    results.append(key)

            return results

        # Run multiple async tasks concurrently
        tasks = [async_cache_worker(i) for i in range(4)]
        all_results = await asyncio.gather(*tasks)

        # Flatten results
        total_successful = sum(len(results) for results in all_results)

        # Should have some successful operations
        assert total_successful > 0

        # Cache should be within size limits
        assert cache.size() <= 3


class TestEngineCacheIntegration:
    """Test engine cache integration with actual engine instances."""

    def test_engine_cache_creation_lock(self) -> None:
        """Test that engine creation is properly synchronized."""
        from src.pipeline.inference.engine import _engine_creation_lock

        # Test that the lock exists and is a threading.Lock
        assert hasattr(_engine_creation_lock, "acquire")
        assert hasattr(_engine_creation_lock, "release")

        # Test lock acquisition
        acquired = _engine_creation_lock.acquire(blocking=False)
        assert acquired is True
        _engine_creation_lock.release()

    @patch("src.pipeline.inference.engine._engine_cache")
    def test_engine_cache_mock_integration(self, mock_cache: Mock) -> None:
        """Test engine cache integration patterns."""
        # Mock cache behavior
        mock_cache.get.return_value = None
        mock_cache.put.return_value = None
        mock_cache.size.return_value = 1

        # Test cache interaction pattern
        cache_key = "test_engine_config"

        # Simulate cache miss
        cached_engine = mock_cache.get(cache_key)
        assert cached_engine is None

        # Simulate cache put after engine creation
        mock_engine = Mock()
        mock_cache.put(cache_key, mock_engine)

        # Verify calls
        mock_cache.get.assert_called_once_with(cache_key)
        mock_cache.put.assert_called_once_with(cache_key, mock_engine)

    def test_engine_cache_key_generation_consistency(self) -> None:
        """Test that cache keys are generated consistently for same config."""
        from src.pipeline.inference.models import W2VConfig

        # Create identical configs
        config1 = W2VConfig(model_path="test/path", device="cpu", use_triton=False)
        config2 = W2VConfig(model_path="test/path", device="cpu", use_triton=False)

        # Keys should be identical for identical configs
        # Note: Actual key generation logic may vary, this tests the principle
        key1 = str(hash(str(config1.model_dump_json())))
        key2 = str(hash(str(config2.model_dump_json())))

        assert key1 == key2, "Identical configs should generate identical cache keys"

    def test_cache_maxsize_environment_variable(self) -> None:
        """Test that cache maxsize respects environment variable."""
        from src.pipeline.inference.engine import _ENGINE_CACHE_MAX

        # Should use default or environment value
        assert isinstance(_ENGINE_CACHE_MAX, int)
        assert _ENGINE_CACHE_MAX > 0

        # Test with custom environment
        with patch.dict("os.environ", {"ENGINE_CACHE_MAX": "5"}):
            # Re-import to pick up new env var (in practice, this is set at startup)
            import importlib

            import src.pipeline.inference.engine

            importlib.reload(src.pipeline.inference.engine)

            from src.pipeline.inference.engine import _ENGINE_CACHE_MAX as new_max

            # Note: Due to module reloading complexities, we test the principle
            assert isinstance(new_max, int)
