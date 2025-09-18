import pytest


def test_cpu_engine_imports() -> None:
    from src.pipeline.inference.engine import Wav2Vec2InferenceEngine  # noqa: F401


def test_triton_engine_import_guard() -> None:
    from src.pipeline.inference.triton_engine import TRITON_AVAILABLE

    if not TRITON_AVAILABLE:
        pytest.skip("tritonclient not installed")

    from src.pipeline.inference.triton_engine import TritonInferenceEngine  # noqa: F401
