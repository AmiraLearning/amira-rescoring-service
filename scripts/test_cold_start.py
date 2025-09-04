#!/usr/bin/env python3
"""Test cold start optimizations for Lambda warm-up scenarios.

This script benchmarks different W2V2 engine initialization strategies
to optimize Lambda cold start performance while ensuring model caching
works correctly for warm invocations.
"""

import statistics
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from loguru import logger
from pydantic import BaseModel, Field

from src.pipeline.inference.engine import Wav2Vec2InferenceEngine
from src.pipeline.inference.models import W2VConfig

DEFAULT_MODEL_PATH: Final[str] = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"


class OptimizationStrategy(StrEnum):
    """Cold start optimization strategies."""

    BASELINE = "baseline"
    FAST_INIT = "fast_init"
    JIT_TRACE = "jit_trace"
    TORCH_COMPILE = "torch_compile"


@dataclass(frozen=True)
class BenchmarkResult:
    """Results from a single benchmark run."""

    strategy: OptimizationStrategy
    times: list[float]

    @property
    def mean(self) -> float:
        """Mean initialization time."""
        return statistics.mean(self.times)

    @property
    def median(self) -> float:
        """Median initialization time."""
        return statistics.median(self.times)

    @property
    def std_dev(self) -> float:
        """Standard deviation of initialization times."""
        return statistics.stdev(self.times) if len(self.times) > 1 else 0.0


class BenchmarkConfig(BaseModel):
    """Configuration for cold start benchmarks."""

    model_path: str = Field(
        default="models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24",
        description="Path to W2V2 model",
    )
    runs: int = Field(default=3, ge=1, description="Number of benchmark runs")

    def create_strategy_config(self, *, strategy: OptimizationStrategy) -> W2VConfig:
        """Create W2V config for specific optimization strategy.

        Args:
            strategy: Optimization strategy to configure.

        Returns:
            W2V configuration for the strategy.
        """
        base_config = {
            "model_path": self.model_path,
            "use_torch_compile": False,
            "use_jit_trace": False,
            "fast_init": False,
        }

        match strategy:
            case OptimizationStrategy.BASELINE:
                pass  # Use base config as-is
            case OptimizationStrategy.FAST_INIT:
                base_config["fast_init"] = True
            case OptimizationStrategy.JIT_TRACE:
                base_config["use_jit_trace"] = True
            case OptimizationStrategy.TORCH_COMPILE:
                base_config["use_torch_compile"] = True

        return W2VConfig(**base_config)


STRATEGIES: list[OptimizationStrategy] = [
    OptimizationStrategy.BASELINE,
    OptimizationStrategy.FAST_INIT,
    OptimizationStrategy.JIT_TRACE,
    OptimizationStrategy.TORCH_COMPILE,
]


class ColdStartBenchmarker:
    """Benchmarks cold start performance across optimization strategies."""

    def __init__(self, *, config: BenchmarkConfig) -> None:
        """Initialize benchmarker.

        Args:
            config: Benchmark configuration.
        """
        self._config: BenchmarkConfig = config
        self._results: dict[OptimizationStrategy, BenchmarkResult] = {}

    def _benchmark_strategy(self, *, strategy: OptimizationStrategy) -> BenchmarkResult:
        """Benchmark a single optimization strategy.

        Args:
            strategy: Strategy to benchmark.

        Returns:
            Benchmark results for the strategy.
        """
        w2v_config = self._config.create_strategy_config(strategy=strategy)
        times: list[float] = []
        engine: Wav2Vec2InferenceEngine | None = None

        logger.info(f"Benchmarking {strategy.value} ({self._config.runs} runs)")

        for run_idx in range(self._config.runs):
            start_time = time.perf_counter()

            engine: Wav2Vec2InferenceEngine = Wav2Vec2InferenceEngine(w2v_config=w2v_config)

            init_time = time.perf_counter() - start_time
            times.append(init_time)

            logger.info(f"  Run {run_idx + 1}: {init_time:.3f}s")

            del engine

        return BenchmarkResult(strategy=strategy, times=times)

    def run_all_benchmarks(self) -> dict[OptimizationStrategy, BenchmarkResult]:
        """Run benchmarks for all optimization strategies.

        Returns:
            Dictionary mapping strategies to their benchmark results.
        """

        for strategy in STRATEGIES:
            try:
                result: BenchmarkResult = self._benchmark_strategy(strategy=strategy)
                self._results[strategy] = result
            except Exception as e:
                logger.error(f"Benchmark failed for {strategy.value}: {e}")
                continue

        return self._results

    def _calculate_improvement(
        self, *, baseline: BenchmarkResult, optimized: BenchmarkResult
    ) -> tuple[float, float]:
        """Calculate performance improvement metrics.

        Args:
            baseline: Baseline benchmark results.
            optimized: Optimized benchmark results.

        Returns:
            Tuple of (speedup_ratio, improvement_percentage).
        """
        speedup = baseline.mean / optimized.mean
        improvement_pct = (speedup - 1) * 100
        return speedup, improvement_pct

    def print_results(self) -> None:
        """Print comprehensive benchmark results."""
        if not self._results:
            logger.error("No benchmark results available")
            return
        baseline_result: BenchmarkResult | None = self._results.get(OptimizationStrategy.BASELINE)
        if not baseline_result:
            logger.error("Baseline results required for comparison")
            return

        logger.info("\n" + "=" * 60)
        logger.info("COLD START BENCHMARK RESULTS")
        logger.info("=" * 60)

        for strategy, result in self._results.items():
            logger.info(f"\n{strategy.value.upper()}:")
            logger.info(f"  Mean: {result.mean:.3f}s")
            logger.info(f"  Median: {result.median:.3f}s")
            logger.info(f"  Std Dev: {result.std_dev:.3f}s")
            logger.info(f"  All times: {[f'{t:.3f}s' for t in result.times]}")

        logger.info("\nIMPROVEMENT ANALYSIS:")
        logger.info("-" * 30)

        for strategy, result in self._results.items():
            if strategy == OptimizationStrategy.BASELINE:
                continue

            speedup, improvement_pct = self._calculate_improvement(
                baseline=baseline_result, optimized=result
            )
            time_saved = baseline_result.mean - result.mean

            logger.info(f"\n{strategy.value} vs baseline:")
            logger.info(f"  Speedup: {speedup:.2f}x")
            logger.info(f"  Improvement: {improvement_pct:+.1f}%")
            logger.info(f"  Time saved: {time_saved:+.3f}s per cold start")


def _create_benchmark_config() -> BenchmarkConfig:
    """Create benchmark configuration from environment/defaults.

    Returns:
        Configured benchmark settings.
    """
    return BenchmarkConfig(model_path=DEFAULT_MODEL_PATH)


def main() -> None:
    """Execute cold start optimization benchmarks."""
    logger.info("Starting cold start optimization benchmarks")

    config = _create_benchmark_config()
    benchmarker = ColdStartBenchmarker(config=config)

    try:
        benchmarker.run_all_benchmarks()
        benchmarker.print_results()
    except Exception as e:
        logger.error(f"Benchmark execution failed: {e}")
        raise


if __name__ == "__main__":
    main()
