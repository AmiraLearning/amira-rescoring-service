"""Simple Pipeline Runner."""

import time
import traceback
from typing import Any
import polars as pl
import typer

from src.pipeline.pipeline import run_activity_pipeline
from utils.config import PipelineConfig, load_config
from loguru import logger
from utils.cleanup import cleanup_pipeline_data

app: typer.Typer = typer.Typer(
    help="Run CPU-GPU Parallel LNS Scoring Pipeline", no_args_is_help=True
)


def run_pipeline_core(*, config: PipelineConfig) -> bool:
    """Core pipeline execution logic.

    Args:
        config: Configuration object.

    Returns:
        True if pipeline completed successfully, False otherwise.
    """
    start_time: float = time.time()

    try:
        logger.info(f"Pipeline config: {config}")

        config_dict: dict[str, Any] = config.dict()
        _: pl.DataFrame = run_activity_pipeline(config_dict)

        total_time: float = time.time() - start_time
        logger.info(f"Total time (including setup): {total_time:.1f}s")

        return True

    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        return False

    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        logger.error(traceback.format_exc())
        return False


@app.command()
def run(
    config_path: str = typer.Option(
        ..., "--config", "-c", help="Path to configuration YAML file"
    ),
    cleanup: bool = typer.Option(
        False,
        "--cleanup",
        help="Clean up audio and result files after pipeline completion",
    ),
) -> None:
    """Run the CPU-GPU Parallel LNS Scoring Pipeline."""
    typer.echo("Scoring Pipeline Runner\n========================")
    typer.echo("This script runs the scoring pipeline for letter names and sounds.\n")

    config_obj: PipelineConfig | None = None
    success: bool = False

    try:
        config_obj: PipelineConfig = load_config(config_path=config_path)
        success = run_pipeline_core(config=config_obj)

        if success:
            typer.echo("Pipeline completed successfully!")
        else:
            typer.echo("Pipeline failed!", err=True)

    except Exception as e:
        typer.echo(f"An unexpected error occurred: {e}", err=True)
        logger.error(f"Pipeline execution failed: {e}")
        logger.error(traceback.format_exc())

    finally:
        if cleanup and config_obj:
            try:
                cleanup_pipeline_data(config=config_obj)
            except Exception as e:
                typer.echo(f"Cleanup failed: {e}", err=True)
                logger.error(f"Cleanup failed: {e}")

        if not success:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
