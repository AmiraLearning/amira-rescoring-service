"""
ORF Rescoring Pipeline - Lambda Entry Points

This module provides simple entry points for the ORF rescoring pipeline
that work with Lambda-based serverless architecture. Each function processes
individual activities without batch operations.
"""

import asyncio
import logging
from typing import Any, cast

from orf_rescoring_pipeline.lambda_handler import handler

logger = logging.getLogger(__name__)


def process_single_activity_entry(
    activity_id: str, env_name: str = "prod2", debug: bool = False
) -> dict[str, Any]:
    """
    Simple entry point for processing a single activity.

    This is a convenience wrapper around the Lambda handler for local testing
    and development purposes.

    Args:
        activity_id: The activity ID to process
        env_name: Environment name (prod2, stage, dev)
        debug: Enable debug mode for detailed analysis

    Returns:
        Processing result with status and details
    """
    event = {"activity_id": activity_id, "env_name": env_name, "debug": debug}

    result = cast(dict[str, Any], asyncio.run(handler(event, None)))

    if result["statusCode"] == 200:
        logger.info(f"Successfully processed activity {activity_id}")
    else:
        logger.error(
            f"Failed to process activity {activity_id}: {result.get('body', 'Unknown error')}"
        )

    return result


if __name__ == "__main__":
    # Example usage for local testing
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <activity_id> [env_name] [debug]")
        print("Example: python pipeline.py ABC123 stage true")
        sys.exit(1)

    activity_id = sys.argv[1]
    env_name = sys.argv[2] if len(sys.argv) > 2 else "prod2"
    debug = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else False

    print(f"Processing activity {activity_id} in {env_name} environment (debug: {debug})")

    result = process_single_activity_entry(activity_id, env_name, debug)

    if result["statusCode"] == 200:
        print("Activity processed successfully")
    else:
        print(f"Activity processing failed: {result}")
        sys.exit(1)
