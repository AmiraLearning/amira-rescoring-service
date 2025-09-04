### High-signal findings

- Critical: async blocking in phrase slicing
  - `utils/phrase_slicing.py`: `reconstitute_and_save_phrase(...)` is synchronous and called directly inside the async `process_activity_into_phrase_sliced_audio(...)`. It performs CPU work and `subprocess.run`, which blocks the event loop.
  - Fix: wrap the entire call in `await asyncio.to_thread(reconstitute_and_save_phrase, ...)` or switch to `asyncio.create_subprocess_exec` for ffmpeg and run the function off-thread.

- S3 streaming body not explicitly closed
  - `infra/s3_client.py`: after `get_object`, code reads `response["Body"]` to EOF but never calls `await body.close()`. While reading to EOF typically closes, explicit close prevents connection pool starvation under errors.
  - Fix: `try/finally: await body.close()` after the read loop, or use `async with` pattern if available.

- Cache race: engine double-create window
  - `src/pipeline/inference/engine.py:get_cached_engine(...)`: checks `if cache_key in _cached_engines` before acquiring the lock, then acquires the lock and checks/creates. Two threads can pass the first check and both create engines.
  - Fix: move the initial membership check inside the lock or use a single lock-guarded path.

- CUDA autocast dtype clarity
  - `src/pipeline/inference/engine.py:_run_model_inference(...)`: When `use_mixed_precision` but not `use_float16`, autocast runs with default dtype, which may be bf16 on some GPUs and fp16 on others.
  - Fix: decide a default (bf16 or fp16) per environment and document; optionally expose an explicit `autocast_dtype` config.

- JIT trace brittleness
  - `src/pipeline/inference/engine.py:_init_jit_trace`: `torch.jit.trace` with a fixed 16000-sample dummy can lock shapes; dynamic sequence models may require scripting or dynamic shapes.
  - Fix: add a config guard or dimension checks; fallback is already present, so at minimum document known safe settings.

- Global cache key composition edge cases
  - `src/pipeline/inference/engine.py:_make_cache_key`: the `hasattr(DeviceType, "CPU")` check is unnecessary and may mask future device variants; model path + dtype/compile/mixed flags should be sufficient.
  - Fix: simplify to `device_hint = w2v_config.device.value`.

- EMF metric robustness improved, but add dimensions consistency
  - Metric calls now guard exceptions. Ensure consistent dimensions across metrics (service, stage/device/model/correlation) as per your lower-priority TODO for dashboards.

- S3 list/head retries added; consider body-size backpressure
  - You have bounded semaphores for list/head/get and configurable buffer sizes. Consider adaptive buffer sizing for large objects or a cap on total in-flight bytes to avoid memory spikes when concurrency is high.

- Phrase slicing subprocess fallback path is still blocking
  - `utils/phrase_slicing.py`: the ffmpeg fallback re-encode path uses `subprocess.run` with potentially large payloads; if run frequently, prefer async subprocess or thread offloading (same item as the first bullet but specifically for the fallback path).

- Safe file cleaning
  - `utils/s3_audio_operations.py`: temp files are unlinked in finally; good. Mirror that explicit cleanup for every path that writes to `/tmp` in async contexts to prevent inode buildup on long runs.

- Triton HTTPS-only enforced
  - `src/pipeline/inference/triton_engine.py`: correctness check for https is good. If you expect internal ALB TLS termination with self-signed certs, consider a configurable CA bundle rather than disabling verification.

- Lambda/CPU quantization toggles
  - `src/pipeline/inference/engine.py:_init_device`: dynamic quantization guarded by `W2V2_QUANTIZE`. Consider exposing a minimum CPU-cores threshold (or model size threshold) to skip quantization when it regresses accuracy/throughput.

- Test coverage focus areas
  - Add tests around: decoder consecutive separators and empty segments; `s3_audio_operations.s3_find` parsing of keys; engine cache concurrency; robust mode token-skip warnings (you already planned these).

- Minor style/consistency
  - Ensure all public functions use keyword-only arguments; most do, but a few helpers are still positional.
  - Replace ad-hoc env parsing with small helpers where repeated (you started doing this in `infra/s3_client.py` config class; consider reusing for other env gates).

If you want, I can produce a short, prioritized edits list and diffs for:
- Making phrase slicing non-blocking in the event loop.
- Closing S3 streaming bodies explicitly.
- Locking the engine cache lookups to avoid double-create.

rsion management
     ☒ Compile comprehensive audit report

⏺ Comprehensive Code Audit Report

  Executive Summary

  I've completed a thorough audit of the Amira Letter Scoring codebase, a sophisticated audio
  processing and machine learning pipeline for speech recognition and phonetic analysis. The system
   processes audio files through AWS infrastructure using both CPU and GPU inference engines.

  Project Architecture Overview

  - Core Pipeline: Speech processing with Wav2Vec2 model and custom ASR alignment
  - Infrastructure: AWS-based with Lambda, SQS, S3, and Triton inference
  - Languages: Python (main), Rust (ASR aligner), TypeScript (CDK)
  - Dependencies: 27 main dependencies, well-managed with uv/pyproject.toml

  Critical Issues (5) - Immediate Action Required

  🔴 Security Vulnerabilities

  1. Command Injection Risk - utils/phrase_slicing.py:408-455
  - subprocess.run() with potentially untrusted input in ffmpeg commands
  - Impact: Remote code execution
  - Fix: Validate all inputs, use shell=False

  2. Path Traversal Vulnerability - utils/phrase_slicing.py:493
  - Unsafe path construction without validation
  - Impact: File system access outside intended directories
  - Fix: Use Path.resolve().is_relative_to() for validation

  3. Race Condition in Engine Cache - src/pipeline/inference/engine.py:526-551
  - Global _cached_engines accessed without synchronization
  - Impact: Data corruption, crashes in concurrent scenarios
  - Fix: Implement proper locking mechanisms

  4. Memory Leak in Audio Caching - utils/audio.py:174-181
  - @lru_cache on Path objects causes unbounded growth
  - Impact: Memory exhaustion over time
  - Fix: Implement cache eviction or remove Path caching

  5. Unsafe Environment Variable Access - lambda/parallel_processor/index.py:40,98,134
  - Missing validation of environment variables
  - Impact: Potential credential exposure
  - Fix: Validate all environment access, use IAM roles

  High Issues (7) - Should Fix Soon

  ⚠️ Architecture & Performance

  6. Monolithic Function - src/pipeline/pipeline.py:242-365
  - 123-line process_activity() function with multiple responsibilities
  - Fix: Break into focused, single-responsibility functions

  7. Global State Management - lambda/parallel_processor/index.py:37-41
  - Multiple global variables without lifecycle management
  - Fix: Implement proper singleton or dependency injection

  8. Synchronous I/O in Async Context - utils/audio.py:97-137
  - Blocking file operations in async functions
  - Fix: Use asyncio.to_thread() for blocking operations

  9. Inefficient S3 Client Pool - infra/s3_client.py:404-427
  - Pool can exceed configured limits
  - Fix: Implement semaphore-based pool management

  10. Bare Exception Handling - src/pipeline/inference/engine.py:454-496
  - Overly broad exception catching masks specific errors
  - Fix: Catch specific exceptions with meaningful messages

  11. Tight Coupling - src/pipeline/audio_prep/engine.py:108-128
  - Direct instantiation violates dependency inversion
  - Fix: Use dependency injection

  12. Silent Metric Dropping - utils/logging.py:87-89
  - EMF metrics can be silently dropped without notification
  - Fix: Add debug logging for dropped metrics

  Medium Issues (7) - Should Address

  📋 Maintainability & Code Quality

  13. Magic Numbers - utils/audio.py:38,140,534
  - Hardcoded values like 64 * 1024 * 1024, 44, 6
  - Fix: Extract to named constants with documentation

  14. Code Duplication - Multiple files
  - Duplicate S3 bucket resolution and config parsing logic
  - Fix: Centralize common functionality

  15. Inconsistent Type Hints - Various files
  - Missing type hints throughout codebase
  - Fix: Add comprehensive type annotations

  16. Resource Management - utils/phrase_slicing.py:108-126
  - Temporary files not guaranteed cleanup in error paths
  - Fix: Use context managers or try/finally blocks

  17. Inconsistent Logging - Various files
  - Mixed logging patterns without correlation IDs
  - Fix: Standardize structured logging

  18. ThreadPool Resource Leak - utils/phrase_slicing.py:535-546
  - Potential resource leak in error scenarios
  - Fix: Ensure proper exception handling

  19. Long Parameter Lists - infra/s3_client.py:554-597
  - Functions with 8+ parameters
  - Fix: Use parameter objects or builder pattern

  Low Issues (5) - Minor Improvements

  20-24: Style consistency, documentation, unused imports, TODO management, naming conventions

  Positive Findings

  ✅ Good Practices Observed

  1. No Star Imports: Clean, explicit imports throughout
  2. No Bare Excepts: Proper exception handling patterns
  3. Modern Python: Good use of Python 3.12 features
  4. Comprehensive Testing: 16 test files covering key functionality
  5. Proper Dependency Management: Well-structured pyproject.toml with locked versions
  6. Security Awareness: No hardcoded secrets found
  7. Clean Architecture: Well-organized module structure

  Statistical Summary

  - Lines of Code: ~5,000+ (excluding dependencies)
  - Python Files: 85+ files analyzed
  - Test Coverage: 16 test files covering core components
  - Exception Handling: 546 try/catch blocks across 43 files
  - Logging: Comprehensive logging in 29 files
  - Dependencies: Modern, up-to-date packages with proper versioning

  Priority Recommendations

  Immediate (Critical)

  1. Fix command injection vulnerability in subprocess calls
  2. Implement path traversal protection
  3. Add synchronization to global cache access
  4. Fix memory leak in audio caching
  5. Validate environment variable access

  Short-term (High)

  1. Refactor monolithic functions
  2. Fix async/sync mixing issues
  3. Implement proper error handling
  4. Address resource pool inefficiencies

  Medium-term

  1. Standardize logging and monitoring
  2. Add comprehensive type hints
  3. Eliminate code duplication
  4. Improve resource management

  Overall Assessment

  The codebase demonstrates solid engineering practices with a well-structured architecture for
  complex audio processing workflows. While several critical security and performance issues
  require immediate attention, the foundation is strong with good test coverage, modern Python
  practices, and clean dependency management.

  Risk Level: Medium-High (due to critical security vulnerabilities)
  Maintainability: Good (with room for improvement in code organization)
  Security Posture: Needs immediate attention (5 critical vulnerabilities)
  Performance: Generally good (some optimization opportunities)

  The team should prioritize addressing the critical security vulnerabilities while planning
  systematic improvements to architecture and maintainability.
