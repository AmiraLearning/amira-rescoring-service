# Roadmap / TODOs

This list captures prioritized follow-ups from the recent audit to improve the baselien system.

**Architecture**
- Decompose `src/pipeline/pipeline.py`: split into data_load, audio_prep, inference, alignment, persistence modules with typed DTOs between stages.
- Replace bare dicts with Pydantic models at boundaries; ensure serialization is explicit.

**S3 / I-O Consolidation**
- Unify helpers: converge on `infra/s3_client.ProductionS3Client` shapes; remove duplicate logic between `utils/phrase_slicing.py` and `utils/s3_audio_operations.py`.
- Add jittered backoff and semaphore limits for head/list ops similar to downloads/uploads.

**Config & CLI**
- Remove hardcoded default dates in `PipelineMetadataConfig`; support required date range or “last N hours” default.
- Strengthen `load_config` validation to fail-fast for required fields (e.g., missing story phrase file when needed, invalid Triton HTTPS).
- Normalize AWS region fields (use `aws_region` single source of truth; derive aliases in one place).

**Inference & Decoding**
- Wire `W2VConfig.use_float16` and document interactions with `use_mixed_precision` and `use_torch_compile`; remove unused flags if not needed.
- Make `torch.cuda.empty_cache()` behavior configurable via env (default off for throughput-sensitive runs).
- Decide on default for `DECODER_ROBUST_MODE`; consider enabling in production with warning logs instead of hard errors on unmatched tokens.

**Reliability & Observability**
- Ensure EMF metrics emitted on all failure paths (replace silent `except: pass` with guarded metric and log).
- Add per-stage SLO/SLI definitions (e2e latency, phrase success rate, queue age, GPU utilization) with error budgets.
- Add soak tests and chaos/fault injection for S3/Athena/Triton network failures.
- Emit `ActivitySuccess=0.0` when `process_single_activity` fails; include `ActivityTotalMs` and `CorrelationId`.
- Emit `AlignFailure=1.0` when alignment falls back or errors; include counts and `CorrelationId`.
- Standardize EMF dimensions across all metrics: `Service`, `Stage`, `Device`, `Model`, `CorrelationId`, `ActivityId`.

**OTel & Logging Consistency**
- Dependencies: add `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-requests`, `opentelemetry-instrumentation-botocore`, and optionally `opentelemetry-instrumentation-aws-lambda` (or use ADOT Lambda layer).
- Bootstrap tracing:
  - Local/CLI: initialize tracer provider in `main.py` with OTLP exporter (endpoint via env), resource attrs `service.name`, `service.version`, `deployment.environment`.
  - Lambda: prefer ADOT Lambda layer with `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`; otherwise call `AwsLambdaInstrumentor().instrument()` early in handler.
- Instrumentation: enable `RequestsInstrumentor` and `BotocoreInstrumentor` (covers boto3/aioboto3); consider `urllib3` if needed by Triton client.
- Custom spans: wrap `run_activity_pipeline`, `prepare_activity_audio`, `perform_single_audio_inference`, `perform_alignment`, and S3 batch ops. Attach attrs: `activityId`, `phraseIndex`, `device`, `modelPath`, `tritonModel`, `s3Bucket`, `keysCount`.
- Log schema & levels:
  - Standard keys everywhere: `service`, `env`, `correlationId`, `activityId`, `phraseIndex`, `phase`, `status`, `latencyMs`.
  - Levels policy: INFO (phase start/finish), DEBUG (internals, per-phrase details), WARNING (transient/retry/fallback), ERROR (failures with stack traces).
  - Correlate logs with traces: enrich loguru records with `trace_id`/`span_id` from OTel context; enable `OTEL_PYTHON_LOG_CORRELATION=true` equivalent.
  - Replace silent `except: pass` with level-appropriate logs and EMF emission (respect `LOG_SAMPLING_RATE`).
- Incremental rollout:
  - Phase 1: add deps, base OTel init, instrument requests/botocore, inject trace IDs into logs, standardize schema/levels.
  - Phase 2: add custom spans + attributes around key phases and per-phrase processing; sample DEBUG.
  - Phase 3 (optional): export selected metrics via OTel metrics and/or reduce EMF duplication.

**Security**
- Formalize threat model and data handling policy for audio/PII; verify encryption at rest/in-transit end-to-end.
- Check in least-privileged IAM policies and review with IaC (CDK) for principle of least privilege.
- Add SBOM/dependency policy and vulnerability scanning in CI (e.g., pip-audit, safety, OSV).

**CI/CD & Quality**
- Add tests for: complete-audio error flag path; AppSync success vs. failure logging; `utils/s3_audio_operations.s3_find` parsing; decoder robust mode.
- Set coverage targets for core layers; add resilience tests for intermittent S3 failures and Triton unavailability.
- Add progressive deploy (canary) and automated rollback hooks; gate releases on health checks.
- Add concurrency test for inference engine cache (multi-thread warm + get) to ensure lock correctness.
- Add decoder edge-case tests (consecutive separators, empty segments, mismatched confidences length).

**Performance**
- Validate thread pools and semaphores under load; benchmark with realistic concurrency (Lambda and local).
- Re-evaluate JIT/compile/AMP combos for best throughput on CPU/GPU; document recommended presets per environment.

**Docs & Ops**
- Update runbooks for new config validation, decoder robustness setting, and S3 helper unification.
- Add cost guardrails and scaling guidance (concurrency limits, batch sizes, Triton auto-scale triggers).
- Add `docs/DEV.md` covering:
  - Environment flags: `DECODER_ROBUST_MODE`, `W2V2_QUANTIZE`, `USE_TRITON`, log sampling.
  - CPU-only vs Triton modes (CDK params, VPC attach, SSM dependency), and local dev flow.

Notes
- Three quick fixes were applied during the audit (not tracked here): AppSync success logging, complete-audio success flag, and S3 list parsing in `utils/s3_audio_operations`.

**Target Maturity**
- Completing this TODO further improves maturity:
  - Modular architecture and typed boundaries enabling safer change.
  - Reliability practices (SLOs, retries/backoff, circuit breakers, soak/chaos tests).
  - Strong observability (OTel tracing + consistent structured logs + EMF metrics).
  - Security (HTTPS-only Triton, least-privileged IAM, KMS options, SBOM/vulnerability scanning).
  - CI/CD rigor (lint/type/coverage gates, resilience tests, canary + rollback health gates).
  - Performance validation and cost guardrails; updated runbooks.

**Beyond:**
- Multi-region/DR: Define RTO/RPO, cross-region replication, failover runbooks and automated drills.
- Policy as Code: Enforce IAM, network egress, artifact signing, and image provenance checks in CI.
- Data Governance: DPIA/threat model, formal retention/erasure workflows, and redaction pipelines for logs/metrics.
- Capacity & Cost: Continuous cost dashboards, autoscaling guardrails, performance/cost regression alerts.
- Provider Contracts: Contract tests with AppSync/Triton; versioned data schemas and migrations; provider SLAs documented.

**Additional Critical Items**
- Secrets & config management: move secrets to AWS Secrets Manager/SSM Parameter Store with rotation; forbid long‑lived secrets in repo/env; add secret scanning in CI (e.g., gitleaks).
- Idempotency & deduplication: implement idempotency keys for activities and S3 writes; consider FIFO queues or a DynamoDB idempotency table; ensure retries are safe.
- Dynamic config & feature flags: introduce AWS AppConfig/SSM‑backed toggles for `use_triton`, decoder robustness, concurrency caps, and fallbacks without redeploy.
- Dependency health & fallbacks: proactive Triton readiness checks and circuit breaker; automatic CPU fallback when remote inference is degraded; health probes for critical deps.
- Model integrity & governance: pin model versions and verify checksums/safetensors; record model hash in metrics/logs; add drift monitoring and pre‑promotion eval gates.
- Privacy & compliance: data classification; log/metric redaction for PII; retention/deletion jobs; access audit trails (CloudTrail/S3 access logs); explicit KMS key policies.
- Rate limiting & quotas: adaptive per‑service rate limits (S3, Athena, AppSync); token‑bucket governors; SQS backpressure to avoid cost/regional throttles.
- Atomic result writes: ensure S3 result writes are atomic/idempotent (`_SUCCESS` only after data); optionally use ETag checks or versioning; consider a processing ledger in DynamoDB.
- Disk space guards: enforce local cache size/TTL; check free disk before downloads to prevent ENOSPC in Lambda/containers.
- Incident response: severity matrix, paging policy, runbook templates, and post‑incident review checklist with log/trace links.
- Container hardening: minimal base images, non‑root user, read‑only FS, drop Linux capabilities, supply‑chain scanning for images.
- Load/capacity testing: add load test harness (Locust/k6) with representative audio; publish baseline throughput/latency and alert on regressions.
- Time/clock correctness: standardize on UTC everywhere; verify EMF timestamps and any cross‑region clock skew handling.
- Error taxonomy: define error codes/classes and map to log fields and metrics for consistent triage and dashboards.
