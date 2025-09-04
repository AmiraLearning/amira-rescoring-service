from utils.config import PipelineConfig

from .engine import AudioPreparationEngine
from .models import (
    ActivityInput,
    ActivityOutput,
    PhraseInput,
    ProcessedPhraseOutput,
)


async def cpu_download_worker(
    *,
    phrases_input: list[PhraseInput],
    activity_id: str,
    config: PipelineConfig,
) -> ActivityOutput:
    engine: AudioPreparationEngine = AudioPreparationEngine(config=config)
    activity_input: ActivityInput = ActivityInput(activityId=activity_id, phrases=phrases_input)

    try:
        # Choose audio preparation method based on config flag
        if config.audio.use_complete_audio:
            return await engine.prepare_activity_audio_with_complete(activity_input=activity_input)
        else:
            return await engine.prepare_activity_audio(activity_input=activity_input)
    finally:
        # Always cleanup resources
        await engine.close()


__all__ = [
    "ActivityInput",
    "ActivityOutput",
    "AudioPreparationEngine",
    "PhraseInput",
    "ProcessedPhraseOutput",
    "cpu_download_worker",
]
