# Roadmap / TODOs

This list captures prioritized follow-ups from the recent audit to improve the baselien system.

**Architecture**
- Decompose `src/pipeline/pipeline.py`: split into data_load, audio_prep, inference, alignment, persistence modules with typed DTOs between stages.
- Replace bare dicts with Pydantic models at boundaries; ensure serialization is explicit.

LOWER PRIORITY STARTS HERE:

- Add per-stage SLO/SLI definitions (e2e latency, phrase success rate, queue age, GPU utilization) with error budgets.
- Add soak tests and chaos/fault injection for S3/Athena/Triton network failures.
- Emit `ActivitySuccess=0.0` when `process_single_activity` fails; include `ActivityTotalMs` and `CorrelationId`.
- Emit `AlignFailure=1.0` when alignment falls back or errors; include counts and `CorrelationId`.
- Standardize EMF dimensions across all metrics: `Service`, `Stage`, `Device`, `Model`, `CorrelationId`, `ActivityId`.
 - Centralize tenacity retry policy helper: a small utility that returns a preconfigured AsyncRetrying/Retrying with jitter, curated retryable exceptions, and env-tunable knobs; reuse across S3 helpers, AppSync, and pipeline calls.

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

## Platform Plan: From Project to Organization-wide Batch ASR

Excellent question. You have built an incredible foundation for a single, high-performance ASR task. Elevating it from a specific project to the go-to platform for batch ASR processing in your organization is a strategic shift. It's less about adding more AWS resources and more about abstraction, developer experience, and multi-tenancy.

Here is a roadmap on how to make your project the go-to solution, building upon your existing CDK stack.

### Phase 1: Generalize and Abstract (From Project to Platform)

Your current stack is purpose-built for AmiraLetterScoring. The first step is to decouple the generic infrastructure from the specific task.

- Isolate the "Workload" from the "Platform":
  - The Platform: Your core infrastructure (ECS Cluster, ASGs with GPUs, SQS queues, ALB, autoscaling logic, monitoring) is the platform. This is the reusable part.
  - The Workload: This is the task-specific part: the container image with the ASR model, the pre/post-processing code, and the specific ModelPath.
  - Action: Refactor your CDK. Create a core AsrBatchPlatformStack that provisions the cluster, networking, and scaling. Then, create a separate AsrWorkloadStack (or a similar construct) that defines an ECS Task Definition and Service for a specific ASR model. This allows new tasks to be onboarded without redeploying the entire cluster.

- Make the Platform Model-Agnostic:
  - Your ModelPath is a CfnParameter, which is a good start. To become a true platform, you need to support any model that can run in a container.
  - Action: Define a standard container contract. For example, your platform could mandate that any ASR container must:
    - Read a job message from an environment variable or a file.
    - Accept audio file locations (S3 URIs) as input.
    - Write its output (transcripts, logs) to a specified S3 prefix.
    - Expose a /health/ready endpoint if it's a service.
  - This way, any team can bring their own ASR model (wav2vec2, Whisper, etc.), package it according to the contract, and run it on your platform.

- Create a Centralized Job Submission API:
  - The ManualEnqueueFunction is a great tool for testing. For a platform, you need a more robust and secure way for users and services to submit jobs.
  - Action: Create a simple internal API using API Gateway with Lambda integration. This API would be the single front door for all ASR batch jobs.
    - POST /jobs: Submits a new batch job. The request body specifies the S3 path to the audio files, the desired "Workload" (e.g., amira-letter-scoring:v1.2 or whisper-large:v3), and where to put the results.
    - GET /jobs/{job_id}: Checks the status of a job.

### Phase 2: Obsess Over Developer Experience (DX)

If your platform is hard to use, no one will adopt it. Your goal is to make running a massive ASR job easier than setting up a single EC2 instance.

- Create a Simple Command-Line Interface (CLI):
  - Most developers prefer a CLI over crafting raw API calls.
  - Action: Build a simple Python CLI (using click or argparse) that wraps your API.

```bash
# Submit a job using the 'amira-letter-scoring' model
asr-platform submit --model amira-letter-scoring:v1.2 \
                    --input s3://my-audio-bucket/unprocessed/ \
                    --output s3://my-results-bucket/run-123/

# Check the status
asr-platform status --job-id <job-id-1234>
```

- Publish Comprehensive Documentation:
  - Quickstart Guide: How to run your first job in 5 minutes using the CLI.
  - The Container Contract: How a team can package their own ASR model to run on the platform.
  - API Reference: Details on the API endpoints.
  - Architecture Overview: A high-level diagram of how the platform works.

- Provide Scaffolding and Templates:
  - Lower the barrier to entry for teams bringing new models.
  - Action: Create a template repository (cookiecutter is great for this) that includes a sample Dockerfile, Python script, and README.md demonstrating how to build a container that complies with your platform's contract. A developer can use this template to get their model running in minutes.

### Phase 3: Implement Governance and Operational Excellence

As more teams use your platform, you need to ensure it remains stable, fair, and cost-effective.

- Implement Cost Allocation and Visibility:
  - Use resource tagging. Tag every resource (ECS services, S3 objects, etc.) with a Project or Team tag passed through the API during job submission.
  - Create a dedicated Cost and Usage Report (CUR) or use AWS Cost Explorer, filtered by these tags, to build a cost dashboard. This allows you to show Team A how much their jobs cost versus Team B.

- Add Multi-Tenancy and Isolation:
  - You don't want a misconfigured job from one team to consume all the GPU resources and starve other teams' jobs.
  - Action:
    - Priority Queues: Evolve your single SQS queue into multiple queues (e.g., high-priority, low-priority). The job submission API can route jobs accordingly.
    - Resource Quotas: Use ECS capacity providers to potentially isolate workloads. You could create separate capacity providers for different teams if strict isolation is needed, though this adds complexity. A simpler start is to implement API-level throttling and quotas (e.g., "Team A can only have 1000 active tasks at a time").

- Enhance Observability for Users:
  - Your current CloudWatch dashboard is excellent for you, the platform owner. Your users will want to see the status of their jobs.
  - Action:
    - Emit custom CloudWatch metrics from your job processing containers with the Project or JobId as a dimension.
    - Create a "Job Status" page or API endpoint that provides logs, progress (e.g., "752 of 1000 files processed"), and a link to the results in S3.
