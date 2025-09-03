import pytest

from src.pipeline.inference.models import W2VConfig
from src.pipeline.inference.triton_engine import TRITON_AVAILABLE, TritonInferenceEngine


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "HTTP://example.com",
        "ftp://example.com",
        "",
        "   ",
        "localhost:8000",
    ],
)
def test_w2vconfig_rejects_non_https_urls(url: str) -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        W2VConfig(use_triton=True, triton_url=url)


@pytest.mark.skipif(not TRITON_AVAILABLE, reason="tritonclient not installed")
def test_triton_engine_rejects_http_url() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        TritonInferenceEngine(w2v_config=W2VConfig(use_triton=True, triton_url="http://x"))
