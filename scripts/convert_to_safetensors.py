#!/usr/bin/env python3
"""Convert PyTorch model to safetensors format for faster loading."""

import statistics
import time
from pathlib import Path
from typing import Final

import torch
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from safetensors.torch import save_file
from transformers import Wav2Vec2ForCTC

DEFAULT_MODEL_PATH: Final[str] = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"
PYTORCH_MODEL_FILENAME: Final[str] = "pytorch_model.bin"
SAFETENSORS_MODEL_FILENAME: Final[str] = "model.safetensors"
BYTES_TO_MB: Final[float] = 1024 * 1024
DEFAULT_BENCHMARK_RUNS: Final[int] = 5


class ModelPaths(BaseModel):
    """Model file paths configuration."""

    model_dir: Path = Field(..., description="Directory containing model files")

    @property
    def pytorch_path(self) -> Path:
        """Path to PyTorch model file."""
        return self.model_dir / PYTORCH_MODEL_FILENAME

    @property
    def safetensors_path(self) -> Path:
        """Path to safetensors model file."""
        return self.model_dir / SAFETENSORS_MODEL_FILENAME

    @field_validator("model_dir")
    @classmethod
    def validate_model_dir_exists(cls, v: Path) -> Path:
        """Validate model directory exists."""
        if not v.exists():
            raise ValueError(f"Model directory does not exist: {v}")
        return v


class ConversionMetrics(BaseModel):
    """Metrics from model conversion process."""

    load_time_seconds: float = Field(..., ge=0, description="Time to load original model")
    convert_time_seconds: float = Field(..., ge=0, description="Time to convert to safetensors")
    original_size_mb: float = Field(..., ge=0, description="Original model size in MB")
    safetensors_size_mb: float = Field(..., ge=0, description="Safetensors model size in MB")

    @property
    def size_ratio(self) -> float:
        """Size ratio of safetensors to original."""
        return self.safetensors_size_mb / self.original_size_mb

    @property
    def total_time_seconds(self) -> float:
        """Total conversion time."""
        return self.load_time_seconds + self.convert_time_seconds


class BenchmarkMetrics(BaseModel):
    """Metrics from loading benchmark comparison."""

    pytorch_times: list[float] = Field(..., min_length=1, description="PyTorch loading times")
    safetensors_times: list[float] = Field(
        ..., min_length=1, description="Safetensors loading times"
    )

    @property
    def pytorch_stats(self) -> dict[str, float]:
        """PyTorch loading statistics."""
        return {
            "mean": statistics.mean(self.pytorch_times),
            "median": statistics.median(self.pytorch_times),
            "std_dev": statistics.stdev(self.pytorch_times) if len(self.pytorch_times) > 1 else 0.0,
        }

    @property
    def safetensors_stats(self) -> dict[str, float]:
        """Safetensors loading statistics."""
        return {
            "mean": statistics.mean(self.safetensors_times),
            "median": statistics.median(self.safetensors_times),
            "std_dev": statistics.stdev(self.safetensors_times)
            if len(self.safetensors_times) > 1
            else 0.0,
        }

    @property
    def speedup_factor(self) -> float:
        """Loading speedup factor."""
        return self.pytorch_stats["mean"] / self.safetensors_stats["mean"]

    @property
    def improvement_percent(self) -> float:
        """Performance improvement percentage."""
        return (self.speedup_factor - 1) * 100


class ModelConverter:
    """Handles PyTorch to safetensors model conversion."""

    def __init__(self, *, paths: ModelPaths) -> None:
        """Initialize converter with model paths.

        Args:
            paths: Model file paths configuration
        """
        self._paths: ModelPaths = paths
        self._validate_pytorch_model()

    def _validate_pytorch_model(self) -> None:
        """Validate PyTorch model file exists."""
        if not self._paths.pytorch_path.exists():
            raise FileNotFoundError(f"PyTorch model not found: {self._paths.pytorch_path}")

    def _load_model_with_timing(self) -> tuple[Wav2Vec2ForCTC, float]:
        """Load PyTorch model with timing measurement."""
        logger.info(f"Loading PyTorch model from {self._paths.model_dir}")
        start_time = time.perf_counter()
        model: Wav2Vec2ForCTC = Wav2Vec2ForCTC.from_pretrained(str(self._paths.model_dir))
        load_time = time.perf_counter() - start_time
        logger.info(f"Model loaded in {load_time:.3f}s")
        return model, load_time

    def _save_as_safetensors(self, *, model: Wav2Vec2ForCTC, output_path: Path) -> float:
        """Save model in safetensors format with timing."""
        logger.info("Converting to safetensors format...")
        start_time = time.perf_counter()
        save_file(model.state_dict(), str(output_path))
        convert_time = time.perf_counter() - start_time
        logger.info(f"Converted in {convert_time:.3f}s")
        return convert_time

    def _calculate_file_sizes(self, *, output_path: Path) -> tuple[float, float]:
        """Calculate file sizes in MB."""
        original_size = self._paths.pytorch_path.stat().st_size / BYTES_TO_MB
        safetensors_size = output_path.stat().st_size / BYTES_TO_MB
        return original_size, safetensors_size

    def convert(self, *, output_path: Path | None = None) -> ConversionMetrics:
        """Convert model to safetensors format.

        Args:
            output_path: Optional custom output path

        Returns:
            Conversion metrics and timing information
        """
        target_path: Path = output_path or self._paths.safetensors_path

        model, load_time = self._load_model_with_timing()
        convert_time: float = self._save_as_safetensors(model=model, output_path=target_path)

        original_size_mb, safetensors_size_mb = self._calculate_file_sizes(output_path=target_path)

        metrics: ConversionMetrics = ConversionMetrics(
            load_time_seconds=load_time,
            convert_time_seconds=convert_time,
            original_size_mb=original_size_mb,
            safetensors_size_mb=safetensors_size_mb,
        )

        self._log_conversion_results(metrics=metrics)
        return metrics

    def _log_conversion_results(self, *, metrics: ConversionMetrics) -> None:
        """Log conversion results."""
        logger.info(f"Original size: {metrics.original_size_mb:.1f}MB")
        logger.info(f"Safetensors size: {metrics.safetensors_size_mb:.1f}MB")
        logger.info(f"Size ratio: {metrics.size_ratio:.3f}")
        logger.info(f"Total time: {metrics.total_time_seconds:.3f}s")


class LoadingBenchmarker:
    """Benchmarks model loading performance comparison."""

    def __init__(self, *, paths: ModelPaths) -> None:
        """Initialize benchmarker with model paths.

        Args:
            paths: Model file paths configuration
        """
        self._paths = paths

    def _clear_model_memory(self, *, model: Wav2Vec2ForCTC) -> None:
        """Clear model from memory and GPU cache."""
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _benchmark_pytorch_loading(self, *, runs: int) -> list[float]:
        """Benchmark PyTorch model loading times."""
        logger.info(f"Benchmarking PyTorch loading ({runs} runs)")
        times: list[float] = []

        for idx in range(runs):
            start = time.perf_counter()
            model: Wav2Vec2ForCTC = Wav2Vec2ForCTC.from_pretrained(str(self._paths.model_dir))
            self._clear_model_memory(model=model)
            elapsed: float = time.perf_counter() - start
            times.append(elapsed)
            logger.debug(f"PyTorch run {idx + 1}: {elapsed:.3f}s")

        return times

    def _benchmark_safetensors_loading(self, *, runs: int) -> list[float]:
        """Benchmark safetensors model loading times."""
        logger.info(f"Benchmarking safetensors loading ({runs} runs)")
        times: list[float] = []

        for idx in range(runs):
            start: float = time.perf_counter()
            model: Wav2Vec2ForCTC = Wav2Vec2ForCTC.from_pretrained(
                str(self._paths.model_dir), use_safetensors=True
            )
            self._clear_model_memory(model=model)
            elapsed: float = time.perf_counter() - start
            times.append(elapsed)
            logger.debug(f"Safetensors run {idx + 1}: {elapsed:.3f}s")

        return times

    def _ensure_safetensors_exists(self) -> None:
        """Ensure safetensors file exists, convert if needed."""
        if not self._paths.safetensors_path.exists():
            logger.info("Safetensors file not found, converting first...")
            converter: ModelConverter = ModelConverter(paths=self._paths)
            converter.convert()

    def benchmark(self, *, runs: int = DEFAULT_BENCHMARK_RUNS) -> BenchmarkMetrics:
        """Benchmark loading times for both formats.

        Args:
            runs: Number of benchmark runs per format

        Returns:
            Benchmark metrics with timing statistics
        """
        self._ensure_safetensors_exists()

        logger.info(f"Benchmarking loading times ({runs} runs each)")

        pytorch_times: list[float] = self._benchmark_pytorch_loading(runs=runs)
        safetensors_times: list[float] = self._benchmark_safetensors_loading(runs=runs)

        metrics: BenchmarkMetrics = BenchmarkMetrics(
            pytorch_times=pytorch_times,
            safetensors_times=safetensors_times,
        )

        self._log_benchmark_results(metrics=metrics)
        return metrics

    def _log_benchmark_results(self, *, metrics: BenchmarkMetrics) -> None:
        """Log comprehensive benchmark results."""
        pytorch_stats: dict[str, float] = metrics.pytorch_stats
        safetensors_stats: dict[str, float] = metrics.safetensors_stats

        logger.info("BENCHMARK RESULTS:")
        logger.info("-" * 40)
        logger.info(
            f"PyTorch - Mean: {pytorch_stats['mean']:.3f}s, "
            f"Median: {pytorch_stats['median']:.3f}s, "
            f"Std Dev: {pytorch_stats['std_dev']:.3f}s"
        )
        logger.info(
            f"Safetensors - Mean: {safetensors_stats['mean']:.3f}s, "
            f"Median: {safetensors_stats['median']:.3f}s, "
            f"Std Dev: {safetensors_stats['std_dev']:.3f}s"
        )
        logger.info(
            f"Speedup: {metrics.speedup_factor:.2f}x "
            f"({metrics.improvement_percent:+.1f}% improvement)"
        )


def convert_model_to_safetensors(
    *, model_path: str, output_path: str | None = None
) -> ConversionMetrics:
    """Convert PyTorch model to safetensors format.

    Args:
        model_path: Path to model directory containing pytorch_model.bin
        output_path: Optional custom output path for safetensors file

    Returns:
        Conversion metrics with timing and size information

    Raises:
        FileNotFoundError: If pytorch_model.bin not found
        ValueError: If model directory doesn't exist
    """
    paths: ModelPaths = ModelPaths(model_dir=Path(model_path))
    converter: ModelConverter = ModelConverter(paths=paths)

    target_path: Path | None = Path(output_path) if output_path else None
    return converter.convert(output_path=target_path)


def benchmark_loading_times(
    *, model_path: str, runs: int = DEFAULT_BENCHMARK_RUNS
) -> BenchmarkMetrics:
    """Benchmark loading times for both model formats.

    Args:
        model_path: Path to model directory
        runs: Number of benchmark runs per format

    Returns:
        Benchmark metrics with timing statistics

    Raises:
        ValueError: If model directory doesn't exist
    """
    paths: ModelPaths = ModelPaths(model_dir=Path(model_path))
    benchmarker: LoadingBenchmarker = LoadingBenchmarker(paths=paths)
    return benchmarker.benchmark(runs=runs)


def _validate_model_path_exists(*, model_path: str) -> None:
    """Validate model path exists and exit if not."""
    if not Path(model_path).exists():
        logger.error(f"Model path {model_path} not found!")
        raise SystemExit(1)


def main() -> None:
    """Execute model conversion and benchmarking pipeline."""
    logger.info("Starting model conversion and benchmarking pipeline")

    _validate_model_path_exists(model_path=DEFAULT_MODEL_PATH)

    logger.info("Phase 1: Converting model to safetensors format...")
    conversion_metrics: ConversionMetrics = convert_model_to_safetensors(
        model_path=DEFAULT_MODEL_PATH
    )

    logger.info("Phase 2: Benchmarking loading performance...")
    benchmark_metrics: BenchmarkMetrics = benchmark_loading_times(model_path=DEFAULT_MODEL_PATH)

    logger.info("PIPELINE COMPLETE:")
    logger.info(f"  Conversion time: {conversion_metrics.total_time_seconds:.3f}s")
    logger.info(f"  Size reduction: {(1 - conversion_metrics.size_ratio) * 100:.1f}%")
    logger.info(f"  Loading speedup: {benchmark_metrics.speedup_factor:.2f}x")


if __name__ == "__main__":
    main()
