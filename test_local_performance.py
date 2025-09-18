#!/usr/bin/env python3
"""Lightweight local smoke checks for imports and basic execution."""

import time


def smoke_imports() -> None:
    start = time.time()
    try:
        import my_asr_aligner  # noqa: F401
        from src.letter_scoring_pipeline.inference.decoder import PhonemeDecoder  # noqa: F401
        from src.letter_scoring_pipeline.pipeline import run_activity_pipeline  # noqa: F401

        elapsed = time.time() - start
        print(f"✅ Imports OK in {elapsed:.2f}s")
    except Exception as e:
        print(f"❌ Import smoke test failed: {e}")


if __name__ == "__main__":
    print("Basic smoke test")
    smoke_imports()
