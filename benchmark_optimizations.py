#!/usr/bin/env python3
"""Benchmark script to test inference engine optimizations."""

import statistics
import time
from typing import Final, cast

import numpy as np
import torchaudio
from amira_pyutils.logging import get_logger
from pydantic import BaseModel

from src.letter_scoring_pipeline.inference.engine import perform_single_audio_inference
from src.letter_scoring_pipeline.inference.models import W2VConfig

logger = get_logger(__name__)

BENCHMARK_RUNS: Final[int] = 5
WARMUP_RUNS: Final[int] = 2
MS_PER_SECOND: Final[int] = 1_000
DEFAULT_ACTIVITY_ID: Final[str] = "A025D9AFCEB711EFB9CA0E57FBD5D8A1"


class BenchmarkResult(BaseModel):
    """Results from a benchmark configuration test."""

    config_name: str
    mean_time_ms: float
    median_time_ms: float
    std_dev_ms: float
    min_time_ms: float
    max_time_ms: float
    speedup_factor: float
    success_rate: float


class BenchmarkSuite(BaseModel):
    """Complete benchmark suite configuration and execution."""

    activity_id: str = DEFAULT_ACTIVITY_ID
    runs: int = BENCHMARK_RUNS
    warmup_runs: int = WARMUP_RUNS

    def _load_test_audio(self) -> np.ndarray:
        """Load test audio array.

        Returns:
            Audio array for benchmarking.
        """
        try:
            phrase_path = (
                f"audio/reconstituted_phrase_audio/DEFAULT/{self.activity_id}/phrase_4.wav"
            )
            speech, sr = torchaudio.load(phrase_path)
            audio_array = cast(np.ndarray, speech[0].numpy())
            logger.info(f"Loaded phrase audio shape: {audio_array.shape}")
            return audio_array
        except Exception as e:
            logger.error(f"Failed to load phrase audio for activity {self.activity_id}: {e}")
            raise

    def _warmup_model(self, *, config: W2VConfig, audio_array: np.ndarray) -> None:
        """Perform warmup runs to initialize model state.

        Args:
            config: W2V configuration to warm up.
            audio_array: Audio data for warmup.
        """
        logger.info(f"Warming up model with {self.warmup_runs} runs...")
        for _ in range(self.warmup_runs):
            try:
                perform_single_audio_inference(
                    audio_array=audio_array, w2v_config=config, use_cache=True
                )
            except Exception as e:
                logger.warning(f"Warmup run failed: {e}")

    def _benchmark_single_config(
        self, *, config: W2VConfig, config_name: str, audio_array: np.ndarray
    ) -> BenchmarkResult:
        """Benchmark a single configuration with statistical analysis.

        Args:
            config: W2V configuration to benchmark.
            config_name: Human-readable name for the configuration.
            audio_array: Audio data for benchmarking.

        Returns:
            Comprehensive benchmark results.
        """
        self._warmup_model(config=config, audio_array=audio_array)

        execution_times: list[float] = []
        successful_runs: int = 0

        logger.info(f"Running {self.runs} benchmark iterations for {config_name}...")

        for run_idx in range(self.runs):
            start_time = time.perf_counter()
            try:
                result = perform_single_audio_inference(
                    audio_array=audio_array, w2v_config=config, use_cache=True
                )
                end_time = time.perf_counter()

                if result.success:
                    execution_time = end_time - start_time
                    execution_times.append(execution_time)
                    successful_runs += 1
                    logger.debug(f"Run {run_idx + 1}: {execution_time * MS_PER_SECOND:.1f}ms")
                else:
                    logger.warning(f"Run {run_idx + 1} failed: inference unsuccessful")

            except Exception as e:
                logger.error(f"Run {run_idx + 1} failed with exception: {e}")

        if not execution_times:
            logger.error(f"No successful runs for {config_name}")
            return BenchmarkResult(
                config_name=config_name,
                mean_time_ms=float("inf"),
                median_time_ms=float("inf"),
                std_dev_ms=0.0,
                min_time_ms=float("inf"),
                max_time_ms=float("inf"),
                speedup_factor=0.0,
                success_rate=0.0,
            )

        times_ms = [t * MS_PER_SECOND for t in execution_times]
        success_rate = successful_runs / self.runs

        # Ensure times_ms is a list[float] for statistics and min/max overloads
        times_list: list[float] = list(times_ms)
        mean_time_ms = statistics.mean(times_list)
        median_time_ms = statistics.median(times_list)
        std_dev_ms = statistics.stdev(times_list) if len(times_list) > 1 else 0.0
        min_time_ms = min(times_list)
        max_time_ms = max(times_list)

        return BenchmarkResult(
            config_name=config_name,
            mean_time_ms=mean_time_ms,
            median_time_ms=median_time_ms,
            std_dev_ms=std_dev_ms,
            min_time_ms=min_time_ms,
            max_time_ms=max_time_ms,
            speedup_factor=1.0,  # Will be calculated relative to baseline
            success_rate=success_rate,
        )

    def _create_benchmark_configurations(self) -> dict[str, W2VConfig]:
        """Create comprehensive set of benchmark configurations.

        Returns:
            Dictionary mapping configuration names to W2VConfig instances.
        """
        return {
            "baseline": W2VConfig(),
            "jit_trace": W2VConfig(use_jit_trace=True),
            "torch_compile_default": W2VConfig(use_torch_compile=True),
            "torch_compile_reduce_overhead": W2VConfig(
                use_torch_compile=True, compile_mode="reduce-overhead"
            ),
            "torch_compile_max_autotune": W2VConfig(
                use_torch_compile=True, compile_mode="max-autotune"
            ),
            "mixed_precision": W2VConfig(use_mixed_precision=True),
            "jit_plus_mixed_precision": W2VConfig(use_jit_trace=True, use_mixed_precision=True),
            "compile_plus_mixed_precision": W2VConfig(
                use_torch_compile=True, use_mixed_precision=True
            ),
            "all_optimizations": W2VConfig(
                use_jit_trace=True, use_torch_compile=True, use_mixed_precision=True
            ),
        }

    def _calculate_speedup_factors(
        self, *, results: list[BenchmarkResult]
    ) -> list[BenchmarkResult]:
        """Calculate speedup factors relative to baseline configuration.

        Args:
            results: List of benchmark results to update.

        Returns:
            Updated results with speedup factors calculated.
        """
        baseline_result = next((r for r in results if r.config_name == "baseline"), None)
        if not baseline_result or baseline_result.mean_time_ms == float("inf"):
            logger.warning("No valid baseline found for speedup calculation")
            return results

        baseline_time = baseline_result.mean_time_ms

        updated_results: list[BenchmarkResult] = []
        for result in results:
            if result.mean_time_ms > 0 and result.mean_time_ms != float("inf"):
                speedup = baseline_time / result.mean_time_ms
            else:
                speedup = 0.0

            updated_result = result.model_copy(update={"speedup_factor": speedup})
            updated_results.append(updated_result)

        return updated_results

    def _display_results(self, *, results: list[BenchmarkResult]) -> None:
        """Display comprehensive benchmark results.

        Args:
            results: List of benchmark results to display.
        """
        logger.info("\n" + "=" * 80)
        logger.info("BENCHMARK RESULTS SUMMARY")
        logger.info("=" * 80)

        sorted_results: list[BenchmarkResult] = sorted(results, key=lambda r: r.mean_time_ms)

        for result in sorted_results:
            if result.mean_time_ms == float("inf"):
                logger.error(f"{result.config_name}: FAILED (0% success rate)")
                continue

            logger.info(
                f"{result.config_name}:\n"
                f"   Mean: {result.mean_time_ms:.1f}ms ± {result.std_dev_ms:.1f}ms\n"
                f"   Median: {result.median_time_ms:.1f}ms\n"
                f"   Range: {result.min_time_ms:.1f}ms - {result.max_time_ms:.1f}ms\n"
                f"   Speedup: {result.speedup_factor:.2f}x\n"
                f"   Success Rate: {result.success_rate:.1%}\n"
            )

        best_result = sorted_results[0]
        if best_result.mean_time_ms != float("inf"):
            logger.info(
                f"WINNER: {best_result.config_name} with {best_result.speedup_factor:.2f}x speedup"
            )

    def run_comprehensive_benchmark(self) -> list[BenchmarkResult]:
        """Execute comprehensive benchmark suite across all configurations.

        Returns:
            List of benchmark results for all configurations.
        """
        logger.info("Starting comprehensive optimization benchmark suite...")

        audio_array = self._load_test_audio()
        configurations = self._create_benchmark_configurations()

        results: list[BenchmarkResult] = []

        for config_name, config in configurations.items():
            logger.info(f"\nBenchmarking configuration: {config_name}")
            try:
                result = self._benchmark_single_config(
                    config=config, config_name=config_name, audio_array=audio_array
                )
                results.append(result)
                logger.info(f"Completed {config_name}: {result.mean_time_ms:.1f}ms average")
            except Exception as e:
                logger.error(f"Failed to benchmark {config_name}: {e}")
                failed_result = BenchmarkResult(
                    config_name=config_name,
                    mean_time_ms=float("inf"),
                    median_time_ms=float("inf"),
                    std_dev_ms=0.0,
                    min_time_ms=float("inf"),
                    max_time_ms=float("inf"),
                    speedup_factor=0.0,
                    success_rate=0.0,
                )
                results.append(failed_result)

        results_with_speedup = self._calculate_speedup_factors(results=results)
        self._display_results(results=results_with_speedup)

        return results_with_speedup


def main() -> None:
    """Execute the comprehensive benchmark suite."""
    benchmark_suite = BenchmarkSuite()
    benchmark_suite.run_comprehensive_benchmark()


if __name__ == "__main__":
    main()
