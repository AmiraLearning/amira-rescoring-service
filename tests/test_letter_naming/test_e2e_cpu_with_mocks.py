from typing import Any

import numpy as np
import polars as pl
import pytest

from src.letter_scoring_pipeline.inference.constants import DeviceType
from src.letter_scoring_pipeline.inference.models import GPUInferenceResult, PhoneticTranscript
from src.letter_scoring_pipeline.pipeline import run_activity_pipeline
from utils.config import PipelineConfig


@pytest.mark.asyncio
async def test_e2e_cpu_with_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PipelineConfig()
    cfg.metadata.activity_id = "test-activity"

    async def _fake_load_activity_data(*, config: PipelineConfig) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "activityId": ["test-activity"],
                "storyId": ["s1"],
                "studentId": ["stu"],
            }
        )

    def _fake_load_story_phrase_data(*, config: PipelineConfig) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "storyId": ["s1", "s1"],
                "phraseIndex": [0, 1],
                "expected_text": [["a"], ["b"]],
                "reference_phonemes": [["æ"], ["b"]],
            }
        )

    async def _fake_cpu_download_worker(
        *, phrases_input: Any, activity_id: str, config: PipelineConfig
    ) -> Any:
        from src.pipeline.audio_prep.models import ActivityOutput, ProcessedPhraseOutput

        phrases = [
            ProcessedPhraseOutput(
                activityId=activity_id,
                phraseIndex=0,
                speech=np.zeros(1600, dtype=np.float32),
            ),
            ProcessedPhraseOutput(
                activityId=activity_id,
                phraseIndex=1,
                speech=np.zeros(1600, dtype=np.float32),
            ),
        ]
        return ActivityOutput(activityId=activity_id, phrases=phrases, success=True)

    # Patch pipeline-level imported symbols
    monkeypatch.setattr("src.pipeline.pipeline.load_activity_data", _fake_load_activity_data)
    monkeypatch.setattr(
        "src.pipeline.pipeline.load_story_phrase_data", _fake_load_story_phrase_data
    )
    monkeypatch.setattr("src.pipeline.pipeline.cpu_download_worker", _fake_cpu_download_worker)

    class _FakeEngine:
        def infer(self, *, input_data):  # type: ignore[no-untyped-def]
            return GPUInferenceResult(
                transcription="",
                pred_tokens=[],
                max_probs=None,
                phonetic_transcript=PhoneticTranscript(elements=["t"], confidences=[0.9]),
                total_duration_ms=0.0,
                preprocess_time_ms=0.0,
                model_inference_time_ms=0.0,
                decode_time_ms=0.0,
                confidence_calculation_time_ms=0.0,
                device=DeviceType.CPU,
                success=True,
                error=None,
                inference_id=input_data.inference_id,
            )

    async def _fake_preload_inference_engine_async(*, w2v_config, warmup=False):  # type: ignore[no-untyped-def]
        return _FakeEngine()

    monkeypatch.setattr(
        "src.pipeline.inference.engine.preload_inference_engine_async",
        _fake_preload_inference_engine_async,
    )

    results = await run_activity_pipeline(config=cfg)
    assert isinstance(results, list)
    assert len(results) == 1
