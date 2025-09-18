import numpy as np

from src.letter_scoring_pipeline.inference.engine import perform_single_audio_inference
from src.letter_scoring_pipeline.inference.models import W2VConfig


def test_e2e_cpu_placeholder() -> None:
    cfg = W2VConfig(use_triton=False, include_confidence=False)
    audio = np.zeros(1600, dtype=np.float32)
    res = perform_single_audio_inference(audio_array=audio, w2v_config=cfg, inference_id="test")
    assert res is not None
