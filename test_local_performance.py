#!/usr/bin/env python3
"""
Test the optimized Lambda processor locally to validate blazing fast performance.
"""

import time
import sys
from pathlib import Path

# Add lambda processor to path
lambda_path = Path(__file__).parent / "lambda" / "parallel_processor"
sys.path.insert(0, str(lambda_path))


def test_cold_start_simulation():
    """Simulate Lambda cold start locally"""
    print("🧪 Testing blazing fast cold start simulation...")

    # Simulate fresh Lambda environment
    import importlib
    import sys

    # Clear any cached modules (simulate cold start)
    modules_to_clear = [
        m for m in sys.modules.keys() if "torch" in m or "transformers" in m
    ]
    for module in modules_to_clear:
        if module in sys.modules:
            del sys.modules[module]

    # Time the cold start
    cold_start = time.time()

    try:
        # Import and initialize (simulate Lambda startup)
        from index import get_optimized_model, get_config  # type: ignore

        # Set required environment variables
        import os

        os.environ["RESULTS_BUCKET"] = "test-bucket"
        os.environ["MODEL_PATH"] = "facebook/wav2vec2-base-960h"

        # Load model (this is the expensive part)
        print("Loading model...")
        model, processor = get_optimized_model()

        cold_start_time = time.time() - cold_start

        if model and processor:
            print(f"✅ Cold start simulation: {cold_start_time:.3f}s")
            print(f"   Model type: {type(model).__name__}")
            print(f"   Processor type: {type(processor).__name__}")

            # Test inference speed
            test_inference_speed(model, processor)

        else:
            print(f"❌ Model loading failed after {cold_start_time:.3f}s")

    except ImportError as e:
        print(f"❌ Import failed: {e}")
        print("   Run from project root: python test_local_performance.py")
    except Exception as e:
        cold_start_time = time.time() - cold_start
        print(f"❌ Cold start failed after {cold_start_time:.3f}s: {e}")


def test_inference_speed(model, processor):
    """Test inference performance"""
    print("\n🚀 Testing inference speed...")

    try:
        import torch
        import numpy as np

        # Create test audio (1 second of random audio)
        test_audio = np.random.randn(16000).astype(np.float32)

        # Time inference
        inference_times = []

        for i in range(3):
            start_time = time.time()

            # Preprocess
            inputs = processor(
                test_audio, sampling_rate=16000, return_tensors="pt", padding=True
            )

            # Inference (eager mode - no compilation)
            with torch.no_grad():
                logits = model(inputs.input_values).logits

            # Decode
            predicted_ids = torch.argmax(logits, dim=-1)
            transcription = processor.batch_decode(predicted_ids)[0]

            inference_time = time.time() - start_time
            inference_times.append(inference_time)

            print(f"   Run {i + 1}: {inference_time:.3f}s -> '{transcription[:50]}...'")

        avg_inference = sum(inference_times) / len(inference_times)
        print(f"✅ Average inference: {avg_inference:.3f}s (pure eager mode)")

    except Exception as e:
        print(f"❌ Inference test failed: {e}")


def test_lambda_handler_locally():
    """Test the actual Lambda handler locally"""
    print("\n🧪 Testing Lambda handler locally...")

    try:
        from index import lambda_handler

        # Create test SQS event
        test_event = {
            "Records": [
                {
                    "body": json.dumps(
                        {
                            "activityId": "test-activity-123",
                            "timestamp": int(time.time()),
                            "source": "local-test",
                        }
                    ),
                    "messageId": "test-msg-123",
                }
            ]
        }

        # Set environment for local test
        import os

        os.environ.setdefault("RESULTS_BUCKET", "test-results-bucket")
        os.environ.setdefault("AUDIO_BUCKET", "")  # Skip audio download for test

        # Time the handler
        handler_start = time.time()
        result = lambda_handler(test_event, None)
        handler_time = time.time() - handler_start

        print(f"✅ Handler completed in {handler_time:.3f}s")
        print(f"   Status: {result.get('statusCode')}")
        print(f"   Response: {result.get('body', '')[:100]}...")

    except Exception as e:
        print(f"❌ Handler test failed: {e}")


if __name__ == "__main__":
    print("⚡ Blazing Fast Lambda Performance Test")
    print("=" * 50)

    # Test cold start
    test_cold_start_simulation()

    # Test handler
    import json

    test_lambda_handler_locally()

    print("\n🎯 Expected Lambda Performance:")
    print("   • Cold start: 0.5-2 seconds")
    print("   • No compilation overhead")
    print("   • Pure eager mode PyTorch")
    print("   • 45,000 concurrent executions")
    print("   • ARM64 Graviton optimization")
