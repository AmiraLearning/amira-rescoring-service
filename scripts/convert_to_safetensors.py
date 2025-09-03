#!/usr/bin/env python3
"""Convert PyTorch model to safetensors format for faster loading."""

import os
import time
from pathlib import Path
from typing import Final

import torch
from loguru import logger
from pydantic import BaseModel, field_validator
from safetensors.torch import save_file
from transformers import Wav2Vec2ForCTC

DEFAULT_MODEL_PATH: Final[str] = "models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24"
PYTORCH_MODEL_FILENAME: Final[str] = "pytorch_model.bin"
SAFETENSORS_MODEL_FILENAME: Final[str] = "model.safetensors"
BYTES_TO_MB: Final[float] = 1024 * 1024
DEFAULT_BENCHMARK_RUNS: Final[int] = 5


class ModelPaths(BaseModel):
    """Model file paths configuration."""

    model_dir: Path

    @property
    def pytorch_path(self) -> Path:
        return self.model_dir / PYTORCH_MODEL_FILENAME

    @property
    def safetensors_path(self) -> Path:
        return self.model_dir / SAFETENSORS_MODEL_FILENAME

    @field_validator("model_dir")
    def validate_model_dir_exists(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"Model directory does not exist: {v}")
        return v


class ConversionResult(BaseModel):
    """Results from model conversion."""

    load_time_seconds: float
    convert_time_seconds: float
    original_size_mb: float
    safetensors_size_mb: float
    size_ratio: float


class BenchmarkResult(BaseModel):
    """Results from loading benchmark."""

    pytorch_times: list[float]
    safetensors_times: list[float]
    pytorch_avg_seconds: float
    safetensors_avg_seconds: float
    speedup_factor: float
    improvement_percent: float


def _validate_pytorch_model_exists(*, paths: ModelPaths) -> None:
    """Validate that PyTorch model file exists."""
    if not paths.pytorch_path.exists():
        raise FileNotFoundError(f"pytorch_model.bin not found in {paths.model_dir}")


def _load_model_with_timing(*, model_dir: Path) -> tuple[Wav2Vec2ForCTC, float]:
    """Load model and return it with loading time."""
    logger.info(f"Loading PyTorch model from {model_dir}")
    start_time = time.time()
    model = Wav2Vec2ForCTC.from_pretrained(str(model_dir))
    load_time = time.time() - start_time
    logger.info(f"Model loaded in {load_time:.2f}s")
    return model, load_time


def _save_model_as_safetensors(*, model: Wav2Vec2ForCTC, output_path: Path) -> float:
    """Save model as safetensors and return conversion time."""
    logger.info("Converting to safetensors format...")
    convert_start = time.time()
    save_file(model.state_dict(), str(output_path))
    convert_time = time.time() - convert_start
    logger.info(f"Converted in {convert_time:.2f}s")
    return convert_time


def _calculate_file_sizes(*, pytorch_path: Path, safetensors_path: Path) -> tuple[float, float]:
    """Calculate file sizes in MB."""
    original_size_mb = pytorch_path.stat().st_size / BYTES_TO_MB
    safetensors_size_mb = safetensors_path.stat().st_size / BYTES_TO_MB
    return original_size_mb, safetensors_size_mb


def _log_conversion_results(*, result: ConversionResult) -> None:
    """Log conversion results."""
    logger.info(f"Original size: {result.original_size_mb:.1f}MB")
    logger.info(f"Safetensors size: {result.safetensors_size_mb:.1f}MB")
    logger.info(f"Size ratio: {result.size_ratio:.2f}")


def convert_model_to_safetensors(
    *, model_path: str, output_path: str | None = None
) -> ConversionResult:
    """Convert a PyTorch model to safetensors format.

    Args:
        model_path: Path to the model directory containing pytorch_model.bin
        output_path: Optional output path for safetensors file

    Returns:
        ConversionResult with timing and size information

    Raises:
        FileNotFoundError: If pytorch_model.bin not found
        ValueError: If model directory doesn't exist
    """
    paths = ModelPaths(model_dir=Path(model_path))
    _validate_pytorch_model_exists(paths=paths)

    final_output_path = Path(output_path) if output_path else paths.safetensors_path

    model, load_time = _load_model_with_timing(model_dir=paths.model_dir)
    convert_time = _save_model_as_safetensors(model=model, output_path=final_output_path)

    original_size_mb, safetensors_size_mb = _calculate_file_sizes(
        pytorch_path=paths.pytorch_path, safetensors_path=final_output_path
    )

    result = ConversionResult(
        load_time_seconds=load_time,
        convert_time_seconds=convert_time,
        original_size_mb=original_size_mb,
        safetensors_size_mb=safetensors_size_mb,
        size_ratio=safetensors_size_mb / original_size_mb,
    )

    _log_conversion_results(result=result)
    return result


def _clear_model_memory(*, model: Wav2Vec2ForCTC) -> None:
    """Clear model from memory and GPU cache."""
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _benchmark_pytorch_loading(*, model_path: str, runs: int) -> list[float]:
    """Benchmark PyTorch model loading times."""
    logger.info(f"Benchmarking PyTorch loading ({runs} runs)")
    times = []

    for i in range(runs):
        start = time.time()
        model = Wav2Vec2ForCTC.from_pretrained(model_path)
        _clear_model_memory(model=model)
        elapsed = time.time() - start
        times.append(elapsed)
        logger.info(f"PyTorch run {i + 1}: {elapsed:.2f}s")

    return times


def _benchmark_safetensors_loading(*, model_path: str, runs: int) -> list[float]:
    """Benchmark safetensors model loading times."""
    logger.info(f"Benchmarking safetensors loading ({runs} runs)")
    times = []

    for i in range(runs):
        start = time.time()
        model = Wav2Vec2ForCTC.from_pretrained(model_path, use_safetensors=True)
        _clear_model_memory(model=model)
        elapsed = time.time() - start
        times.append(elapsed)
        logger.info(f"Safetensors run {i + 1}: {elapsed:.2f}s")

    return times


def _calculate_benchmark_statistics(
    *, pytorch_times: list[float], safetensors_times: list[float]
) -> tuple[float, float, float, float]:
    """Calculate benchmark statistics."""
    pytorch_avg = sum(pytorch_times) / len(pytorch_times)
    safetensors_avg = sum(safetensors_times) / len(safetensors_times)
    speedup = pytorch_avg / safetensors_avg
    improvement_percent = (speedup - 1) * 100
    return pytorch_avg, safetensors_avg, speedup, improvement_percent


def _log_benchmark_results(*, result: BenchmarkResult) -> None:
    """Log benchmark results."""
    logger.info("Benchmark Results:")
    logger.info(f"PyTorch average: {result.pytorch_avg_seconds:.2f}s")
    logger.info(f"Safetensors average: {result.safetensors_avg_seconds:.2f}s")
    logger.info(f"Speedup: {result.speedup_factor:.2f}x ({result.improvement_percent:.1f}% faster)")


def benchmark_loading_times(
    *, model_path: str, runs: int = DEFAULT_BENCHMARK_RUNS
) -> BenchmarkResult:
    """Benchmark loading times for both formats.

    Args:
        model_path: Path to the model directory
        runs: Number of benchmark runs

    Returns:
        BenchmarkResult with timing statistics

    Raises:
        FileNotFoundError: If safetensors file doesn't exist
    """
    paths = ModelPaths(model_dir=Path(model_path))

    if not paths.safetensors_path.exists():
        logger.info("Safetensors file not found, converting first...")
        convert_model_to_safetensors(model_path=model_path)

    logger.info(f"Benchmarking loading times ({runs} runs each)")

    pytorch_times = _benchmark_pytorch_loading(model_path=str(paths.model_dir), runs=runs)
    safetensors_times = _benchmark_safetensors_loading(model_path=str(paths.model_dir), runs=runs)

    pytorch_avg, safetensors_avg, speedup, improvement_percent = _calculate_benchmark_statistics(
        pytorch_times=pytorch_times, safetensors_times=safetensors_times
    )

    result = BenchmarkResult(
        pytorch_times=pytorch_times,
        safetensors_times=safetensors_times,
        pytorch_avg_seconds=pytorch_avg,
        safetensors_avg_seconds=safetensors_avg,
        speedup_factor=speedup,
        improvement_percent=improvement_percent,
    )

    _log_benchmark_results(result=result)
    return result


def _validate_model_path_exists(*, model_path: str) -> None:
    """Validate that model path exists."""
    if not os.path.exists(model_path):
        logger.error(f"Model path {model_path} not found!")
        exit(1)


def main() -> None:
    """Main execution function."""
    logger.info("Starting model conversion and benchmarking")

    _validate_model_path_exists(model_path=DEFAULT_MODEL_PATH)

    logger.info("Converting model to safetensors format...")
    convert_model_to_safetensors(model_path=DEFAULT_MODEL_PATH)

    logger.info("Benchmarking loading performance...")
    benchmark_loading_times(model_path=DEFAULT_MODEL_PATH)


if __name__ == "__main__":
    main()
