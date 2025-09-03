import pytest

try:
    from src.pipeline.inference.triton_engine import TRITON_AVAILABLE
except Exception:
    TRITON_AVAILABLE = False


@pytest.mark.skipif(not TRITON_AVAILABLE, reason="tritonclient not installed")
def test_triton_engine_available() -> None:
    # Placeholder: real test would mock Triton client and return fake logits
    # TODOD: mock Triton client and return fake logits
    assert TRITON_AVAILABLE is True
