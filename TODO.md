# Roadmap

This document turns our backlog into a focused, outcome‑oriented roadmap. Each phase lists goals, concrete deliverables, and acceptance criteria. Timeboxes are indicative for prioritization, not commitments.

## Phase 0 — Foundations (Done / Ongoing)
- Image‑based Lambda with pre‑cached model, no VPC, low cold‑start
  - Acceptance: `AmiraLambdaParallelStack` deploys; first invocation < ~60s when ECR cached
- Parallelism via platform scale, not in‑function concurrency
  - MAX_CONCURRENCY=1; SQS batchSize=1
- Strict HTTPS to ALB and HTTPS‑only Triton client
  - Acceptance: Triton ALB on 443 with ACM cert; client rejects `http://`
- CI hardening: Node 20, uv caching, concurrency groups, OIDC deploys
  - Acceptance: All workflows green on main; no overlapping deploys

## Phase 1 — Performance & Latency (Week 1–2)
- Lambda handler efficiency
  - Reuse global config and clients across warm invocations (Done)
  - BATCH_ALL_PHRASES=true for single‑call inference per activity (Done)
  - Acceptance: p50 end‑to‑end per activity reduced vs. baseline
- Rust aligner speedups (see section below)
  - Acceptance: 20–30% wall‑clock improvement on typical inputs; reduced allocations

## Phase 2 — TLS End‑to‑End for Triton Targets (Week 2)
- Sidecar TLS (fast path)
  - Nginx/Envoy sidecar terminates TLS, proxied to 127.0.0.1:8000
  - HTTPS Target Group; health checks over TLS
  - Acceptance: All hops TLS; no plaintext inside the VPC data plane
- Productionization (optional)
  - Secrets Manager for cert/key; rotation runbook and alarms
  - Acceptance: Certs rotated without downtime; alarms on expiry

## Phase 3 — Observability & SLOs (Week 2–3)
- Metrics
  - Lambda: preprocess/model/decode/total durations; batch sizes; failures; SQS handle times
  - ECS/Triton: request rate, p95 latency, decode errors, empty‑token rate; DCGM GPU metrics
  - Acceptance: Dashboards visualize key SLIs; alarms on SLO breach paths
- Tracing & logs
  - Correlation IDs across Athena→S3→Lambda/ECS
  - Structured logs with sampling policy
  - Acceptance: A single request trace reconstructs the full path

## Phase 4 — Data Contracts (Week 3–4)
- Versioned schemas
  - AppSync mutation IO; S3 Parquet results; segment/manifest schemas
  - Contract tests in CI, compatibility checks on change
  - Acceptance: CI fails on incompatible schema changes

## Phase 5 — Cost & Scaling (Week 4)
- Autoscaling and backpressure
  - ECS scaling on p95 latency and RequestCountPerTarget
  - Lambda reserved concurrency + SQS redrive and DLQ alarms
- Storage lifecycle and cleanup
  - Results lifecycle; Athena staging cleanup guarantees
  - Acceptance: No unbounded S3/NAT costs; alarms on anomalies

## Phase 6 — Tests & Quality Gates (Week 4–5)
- Unit tests
  - Phoneme pipeline: `decoder.py`, `phonetics.py`
  - Rust bindings round‑trip (`my_asr_aligner`) with goldens
- Engine tests
  - CPU path incl. MPS→CPU fallback; Triton path with fake logits
- Integration tests
  - E2E CPU path with mocked S3/Athena; activity merge correctness
- Coverage: ≥80% core logic with CI gating on critical paths

---

## Rust Aligner Performance Optimization
- Current algorithm: Myers diff via `similar` crate (O((m+n)D), worst O(m*n))

- Bottlenecks
  - Vector allocations of string slices
  - `Vec` used as queue; `remove(0)` O(n)
  - Repeated string allocations for "-"
  - Unnecessary cloning from `hyp_phons`
  - `.drain(..)` where iteration suffices

- Optimization strategy
  1. Pre‑allocate result vectors with capacity (|ref| + |hyp|)
  2. Use `VecDeque` for queue semantics; `pop_front()` O(1)
  3. Avoid string allocations (static `DASH`; use `Cow<str>` when helpful)
  4. Process `diff.ops()` directly; minimize nested iteration
  5. Early exits for identical sequences
  6. Cache `is_error` results
  7. Optional bounded edit distance to bail early

- Expected gains
  - Memory: ~30–40% fewer allocations
  - Speed: ~20–30% typical speedup; better constants in worst case

- Advanced optimizations (future)
  1. Algorithm improvements
     - Consider Wagner-Fischer dynamic programming as alternative to Myers diff for better worst-case complexity
     - Implement phonetic similarity scoring beyond exact matching (e.g., Levenshtein distance on IPA features)
  2. Data structure improvements
     - Make phoneme mappings configurable via JSON/YAML instead of hardcoded HashMap
     - Add weighted acceptance rules based on empirical mispronunciation frequency data
  3. Parallelization
     - Use Rayon for parallel batch processing of multiple activities
     - SIMD optimizations for string comparisons on large batches
  4. Testing & quality
     - Add property-based testing with proptest crate for edge case discovery
     - Fuzzing harness for input validation
     - Performance regression benchmarks in CI
  5. Better abstractions
     - Extract common result-building patterns (push to results, errors, confidence arrays)
     - Create PhonemeMatch enum (Exact, Accepted, Error) for cleaner control flow

## TLS for Triton (End‑to‑End)
- Sidecar TLS (quickest path)
  - Add Nginx/Envoy sidecar; self‑signed cert at startup; HTTPS target group + health checks
- Managed secret path
  - Cert/key in Secrets Manager; hot reload; alarms and rotation policy
- ACM Private CA (optional)
  - Private CA for internal DNS; issue certs; distribute trust chain
- CDK switches
  - `EnableTargetTLS`, `TritonTargetCertSecretArn` for HTTPS target and secret grant
- Client enforcement
  - Triton client requires `https://`; rejects `http://`

## Tests (detail)
- CPU/Triton engines, phoneme modules, aligner bindings, and E2E with mocks; coverage ≥80%

## Observability (detail)
- Dashboards for Lambda, SQS, ECS/Triton; SLOs, error budgets, alert routing, and runbooks

## Cost & Scaling (detail)
- ECS scale on p95 and request count; Lambda concurrency and DLQ alarms; VPC endpoints coverage

## Data Contracts (detail)
- Versioned schemas, CI contract tests, lineage docs, retention policy, backfill procedures

---

### Milestones & Acceptance
- M1 (2 weeks): Phase 1 + Phase 2 (sidecar TLS fast path) in prod; dashboards seeded
- M2 (4 weeks): Phases 3–5 in place; CI contract tests and cost controls active
- M3 (5–6 weeks): Tests ≥80% coverage, rotation/runbooks finalized; optional Private CA

Owners: TBD per workstream (Infra/CDK, Pipeline, Rust, Observability)
Risks: GPU Spot capacity, model size growth, Triton decoding parity

---

## Checklist (Must‑Have)
- Security
  - HTTPS from caller→ALB and ALB→target (sidecar TLS); Secrets Manager for cert/key with rotation
- Observability & SLOs
  - Dashboards for Lambda/SQS/ECS/Triton with SLOs and alert thresholds (p95 latency, error rate, DLQ depth)
  - Alarms wired to notifications; clear ownership and escalation
  - Runbooks for common alerts (queue backlog, Triton unhealthy, cert rotation failure)
- Tests & Quality Gates
  - Unit/integration tests; ≥80% core coverage
  - Contract tests in CI; schema compatibility gating
- Cost & Scaling
  - ECS autoscaling on p95 latency and RequestCountPerTarget
  - Lambda reserved concurrency; SQS backpressure and DLQ alarms
  - Storage lifecycle and Athena staging cleanup jobs
- Deployment & Operations
  - OIDC‑based deployments; non‑overlapping deploys with concurrency guards
  - Safe rollback procedure documented; change review checklist
