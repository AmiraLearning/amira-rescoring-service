import types
from typing import Any

import numpy as np
import pytest
import torch

from src.letter_scoring_pipeline.inference.engine import Wav2Vec2InferenceEngine
from src.letter_scoring_pipeline.inference.models import InferenceInput, W2VConfig


class DummyProcessor:
    def __call__(self, inputs: Any, sampling_rate: int, return_tensors: str, padding: bool) -> Any:
        class BF:
            def __init__(self) -> None:
                self.input_values = torch.zeros((1, 10), dtype=torch.float32)

        return BF()

    @property
    def tokenizer(self) -> Any:
        class T:
            def convert_ids_to_tokens(self, arr: Any) -> list[str]:
                return ["t"] * len(arr)

        return T()

    def batch_decode(self, ids: Any) -> list[str]:
        return ["t"]


class DummyModel(torch.nn.Module):
    def forward(self, x: Any) -> Any:
        return types.SimpleNamespace(logits=torch.zeros((1, 1, 1), dtype=torch.float32))


@pytest.mark.skipif(not hasattr(torch, "inference_mode"), reason="torch not available")
def test_mps_to_cpu_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = W2VConfig(use_triton=False, include_confidence=False)

    engine = Wav2Vec2InferenceEngine(
        w2v_config=cfg,
        model_instance=DummyModel(),
        processor_instance=DummyProcessor(),
    )

    # Force device to mps to trigger fallback branch on error
    engine._device = torch.device("mps")

    raised_once = {"flag": False}

    def _run_model_inference_raise(*, input_values: Any) -> Any:
        if not raised_once["flag"]:
            raised_once["flag"] = True
            raise RuntimeError("convolution_overrideable not implemented")
        return types.SimpleNamespace(
            logits=torch.zeros((1, 1, 1), dtype=torch.float32), model_inference_time_ms=0.0
        )

    # Patch the private method to raise once
    monkeypatch.setattr(engine, "_run_model_inference", _run_model_inference_raise)

    audio = np.zeros(16, dtype=np.float32)
    res = engine.infer(input_data=InferenceInput(audio_array=audio, inference_id="x"))
    assert res.success is True
