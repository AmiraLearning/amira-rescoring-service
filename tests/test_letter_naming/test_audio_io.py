import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import torch


def test_audio_read_timeout_raises(monkeypatch: Any, tmp_path: Path) -> None:
    os.environ["AUDIO_READ_TIMEOUT_SEC"] = "1"

    import utils.audio as audio_mod

    # Create a dummy file path
    wav_path = tmp_path / "dummy.wav"
    wav_path.write_bytes(b"RIFF0000WAVEfmt ")

    def slow_load(path: str) -> Any:
        time.sleep(2)
        raise RuntimeError("should not reach")

    monkeypatch.setattr("utils.audio.torchaudio.load", slow_load)

    with pytest.raises(TimeoutError):
        audio_mod._load_audio_file(file_path=wav_path)


def test_audio_mmap_path_used(monkeypatch: Any, tmp_path: Path) -> None:
    import utils.audio as audio_mod

    # Force mmap path by lowering threshold
    monkeypatch.setattr(audio_mod, "AUDIO_MMAP_THRESHOLD_BYTES", 0)

    wav_path = tmp_path / "dummy_large.wav"
    wav_path.write_bytes(b"RIFF0000WAVEfmt ")

    def fake_wav_read(path: str, mmap: bool = False) -> Any:
        assert mmap is True
        sr = 16000
        data = np.zeros((16000,), dtype=np.int16)
        return sr, data

    monkeypatch.setattr("utils.audio.scipy.io.wavfile.read", fake_wav_read, raising=False)

    tensor, sr = audio_mod._load_audio_file(file_path=wav_path)
    assert sr == 16000
    assert isinstance(tensor, torch.Tensor)
    assert tensor.ndim == 2
    assert tensor.shape[0] == 1
