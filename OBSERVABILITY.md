# Observability

## Metrics & SLOs
- Namespaces: Amira/Inference, Amira/Activity, Amira/Jobs
- Key SLOs (p95): InferenceTotalMs, ActivityTotalMs
- Supporting: PreprocessMs, ModelMs, DecodeMs, ProcessingTime

## Dashboards
- Lambda: AmiraLambdaParallel (SQS depth/age, Lambda errors/latency, p95 activity/inference)
- Triton: AmiraGpuTriton (GPU util, Triton p95, queue latency, failures)

## Alarms
- Queue depth/age, Lambda errors, DLQ depth
- Triton p95 latency high, GPU util high/low
- Secrets rotation disabled/failing

## Tracing
- CorrelationId propagates via:
  - SQS messageId → Lambda logs (contextualize)
  - EMF dimensions (Activity/Inference)
  - S3 metadata (results.json)
  - AppSync header `x-correlation-id`

## Logs
- JSON logs (LOG_JSON=true)
- Optional sampling via LOG_SAMPLING_RATE (0.0–1.0)
