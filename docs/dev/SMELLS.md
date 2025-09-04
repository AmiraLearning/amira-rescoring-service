### Outstanding Items for Future Work

- S3 body-size backpressure optimization
  - Consider adaptive buffer sizing for large objects or a cap on total in-flight bytes to avoid memory spikes when concurrency is high.

- EMF metric dimensions consistency
  - Ensure consistent dimensions across all metrics (service, stage, device, model, correlation) for better dashboarding.

- CPU quantization threshold tuning
  - Consider exposing a minimum CPU-cores threshold to skip quantization when it regresses accuracy/throughput.

- Test coverage expansion
  - Add tests for: decoder consecutive separators and empty segments; `s3_audio_operations.s3_find` parsing; engine cache concurrency; robust mode token-skip warnings.

- Style consistency improvements
  - Ensure all public functions use keyword-only arguments
  - Apply consistent code style using formatters and linters

- Performance optimizations
  - Address JIT trace input shape limitations in engine.py
  - Implement proper bounds checking for audio array sizes
