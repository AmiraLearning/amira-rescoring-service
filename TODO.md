# Roadmap

This document turns our backlog into a focused, outcome‑oriented roadmap. Each phase lists goals, concrete deliverables, and acceptance criteria. Timeboxes are indicative for prioritization, not commitments.

## Phase 2 — TLS End‑to‑End for Triton Targets (Week 2)
 - Productionization (optional)
  - Secrets Manager for cert/key; rotation runbook and alarms
  - Acceptance: Certs rotated without downtime; alarms on expiry

## Phase 5 — Cost & Scaling (Week 4)
 - Autoscaling and backpressure

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
