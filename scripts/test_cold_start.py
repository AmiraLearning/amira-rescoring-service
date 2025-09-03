#!/usr/bin/env python3
"""Test cold start optimizations."""

import time
from typing import Final

from loguru import logger

from src.pipeline.inference.engine import Wav2Vec2InferenceEngine
from src.pipeline.inference.models import W2VConfig

RUNS: Final[int] = 3
DEFAULT_MODEL_PATH: Final[str] = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"


def benchmark_cold_start(*, config: W2VConfig, label: str) -> list[float]:
    """Benchmark cold start times with given configuration.

    Args:
        config: W2V configuration to test
        label: Label for this benchmark

    Returns:
        List of initialization times
    """
    times = []
    logger.info(f"Benchmarking {label} ({RUNS} runs)")

    for i in range(RUNS):
        start_time = time.time()

        # Create engine (simulates Lambda cold start)
        engine = Wav2Vec2InferenceEngine(w2v_config=config)

        init_time = time.time() - start_time
        times.append(init_time)
        logger.info(f"  Run {i + 1}: {init_time:.2f}s")

        # Cleanup
        del engine

    return times


def main() -> None:
    """Test cold start optimizations."""
    logger.info("Testing cold start optimizations")

    baseline_config = W2VConfig(
        model_path=DEFAULT_MODEL_PATH, use_torch_compile=False, use_jit_trace=False, fast_init=False
    )

    # Fast init configuration
    fast_init_config = W2VConfig(
        model_path=DEFAULT_MODEL_PATH,
        use_torch_compile=False,
        use_jit_trace=False,
        fast_init=True,  # Enable fast initialization
    )

    # Benchmark baseline
    baseline_times = benchmark_cold_start(config=baseline_config, label="Baseline")

    # Benchmark with optimizations
    optimized_times = benchmark_cold_start(config=fast_init_config, label="Fast Init")

    # Calculate improvements
    baseline_avg = sum(baseline_times) / len(baseline_times)
    optimized_avg = sum(optimized_times) / len(optimized_times)
    speedup = baseline_avg / optimized_avg
    improvement = (speedup - 1) * 100

    logger.info("\n" + "=" * 50)
    logger.info("COLD START BENCHMARK RESULTS")
    logger.info("=" * 50)
    logger.info(f"Baseline average: {baseline_avg:.2f}s")
    logger.info(f"Fast init average: {optimized_avg:.2f}s")
    logger.info(f"Speedup: {speedup:.2f}x ({improvement:.1f}% faster)")
    logger.info(f"Time saved: {(baseline_avg - optimized_avg):.2f}s per cold start")


if __name__ == "__main__":
    main()
