#!/usr/bin/env python3
"""
Profiling utility script for the pipeline.
Saves profiling output to prof/ directory.
"""

import cProfile
import sys
from pathlib import Path


def profile_main(script_args: list[str], output_name: str = "profile"):
    """Profile main.py with given arguments and save to prof/ directory."""
    prof_dir = Path(__file__).parent.parent / "prof"
    prof_dir.mkdir(exist_ok=True)

    output_file = prof_dir / f"{output_name}.prof"

    # Prepare command to profile
    sys.argv = ["main.py", *script_args]

    # Import and run main
    sys.path.insert(0, str(Path(__file__).parent.parent))

    profiler = cProfile.Profile()

    try:
        profiler.enable()
        import main

        main.app()
    except SystemExit:
        pass  # Expected from typer
    finally:
        profiler.disable()
        profiler.dump_stats(str(output_file))
        print(f"Profile saved to: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/profile_runner.py <main.py args...>")
        print(
            "Example: python scripts/profile_runner.py --activity-id A025D9AFCEB711EFB9CA0E57FBD5D8A1"
        )
        sys.exit(1)

    # Use activity ID or timestamp as profile name
    profile_name = "pipeline"
    for i, arg in enumerate(sys.argv):
        if arg == "--activity-id" and i + 1 < len(sys.argv):
            profile_name = f"activity_{sys.argv[i + 1][:8]}"
            break

    profile_main(sys.argv[1:], profile_name)
