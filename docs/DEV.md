# Developer Guide

## Modes

- CPU-only: set `USE_TRITON=false`. Install extras `.[cpu]`.
- Triton mode: set `USE_TRITON=true` and `TRITON_URL=https://...`, `TRITON_MODEL=w2v2`.

## Key environment variables

- Inference
  - `PIPELINE_MAX_CONCURRENCY`: default 4; caps concurrent activity processing.
  - `W2V2_QUANTIZE`: when `true` and CPU, applies dynamic quantization.
  - `ALIGNER_CONFIDENCE_WEIGHTING`: when `true`, enables confidence-weighted alignment.
  - `DECODER_ROBUST_MODE`: default `true`; when enabled, unmatched tokens are skipped with warnings.
  - `CUDA_EMPTY_CACHE`: default `false`; when `true`, calls `torch.cuda.empty_cache()` after inference.
  - `USE_MIXED_PRECISION` / `use_mixed_precision` in config: enables autocast. `use_float16` forces fp16 dtype for CUDA autocast.

- S3 Client
  - `S3_WARM_CLIENTS`: number of pooled clients to pre-create (default 2).
  - `S3_USE_HEAD_FOR_PROGRESS`: show progress totals by using HEAD before GET.
  - `S3_CLIENT_POOL_SIZE`: size of S3 client pool.
  - `S3_RETRY_MAX` (alias `S3_MAX_RETRIES`): max attempts.
  - `S3_RETRY_BASE` (alias `S3_RETRY_BACKOFF_BASE`): base for randomized exponential backoff.
  - `S3_RETRY_MAX_BACKOFF` (alias `S3_RETRY_BACKOFF_MAX`): backoff cap in seconds.
  - `S3_MAX_CONCURRENT_DOWNLOADS` / `S3_MAX_CONCURRENT_UPLOADS` / `S3_MAX_CONCURRENT_OPERATIONS` tune semaphores.
  - `S3_MAX_CONNECTIONS` / `S3_MAX_CONNECTIONS_PER_HOST` configure HTTP connection pooling.
  - `S3_CONNECTION_TIMEOUT` / `S3_READ_TIMEOUT` configure timeouts.

- Audio I/O
  - `AUDIO_READ_TIMEOUT_SEC`: per-file read timeout (default 10s). Uses `signal.alarm` on main thread; thread timeout off-main thread.
  - `AUDIO_MMAP_THRESHOLD_BYTES`: files ≥ threshold use mmap path via `scipy.io.wavfile.read(..., mmap=True)`.

- Triton
  - `TRITON_URL`: must be `https://...` when `use_triton=true`.
  - `TRITON_MODEL`: model name (default `w2v2`).

## Running tests

- CPU-only:
  - `PYTHONPATH=. uv run pytest -q --ignore=cdk/cdk.out --ignore=cdk/dist --ignore=cdk/lib --ignore=cdk/bin`

## Notes

- Deletes disabled in S3 client by default for safety.
- EMF metrics emit activity and alignment stats; failures emit `ActivitySuccess=0.0` and `AlignFailure=1.0`.
