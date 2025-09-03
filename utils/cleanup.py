import shutil
from pathlib import Path

from loguru import logger

from utils.config import PipelineConfig


def cleanup_pipeline_data(*, config: PipelineConfig) -> bool:
    """Clean up downloaded audio files and result files.

    Args:
        config: Configuration object containing directory paths.

    Returns:
        True if cleanup successful, False otherwise.
    """
    directories: dict[str, Path] = {"Audio": config.audio.audio_dir}
    # Only remove results directory when audit mode is enabled to prevent data loss
    results_path: Path = Path(config.result.output_dir)
    if config.result.audit_mode:
        directories["Results"] = results_path
    else:
        logger.info(
            f"Skipping deletion of results at {results_path} because audit_mode is disabled"
        )

    dirs_to_clean: dict[str, Path] = {
        name: path for name, path in directories.items() if path.exists()
    }

    if not dirs_to_clean:
        logger.info("No files to clean up.")
        return True

    success: bool = True
    for name, path in dirs_to_clean.items():
        try:
            logger.info(f"Removing {name}...")
            shutil.rmtree(path)
            logger.info(f"{name} removed")
        except OSError as e:
            logger.error(f"Failed to remove {name}: {e}")
            success = False

    return success
