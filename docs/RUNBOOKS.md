# Runbooks

## Queue Backlog
- Alert: SQS ApproximateNumberOfMessagesVisible threshold breached
- Actions:
  1. Check Lambda throttles and DLQ depth in dashboard
  2. Increase reserved concurrency or reduce batch size as needed
  3. Verify downstream (Triton/ECS) health and scale-out status
  4. Redrive DLQ if needed: `uv run python scripts/sqs_redrive_dlq.py --dlq-url <dlq> --dest-url <queue> --max 500`

## Triton Unhealthy
- Alert: Target group health check failures or p95 latency alarm
- Actions:
  1. Inspect ECS service events and task logs
  2. Verify ALB → sidecar 8443 connectivity; check cert rotation status
  3. Scale out ECS or roll tasks

## Cert Rotation Failure
- Alert: Rotation job errors / cert nearing expiry
- Actions:
  1. Rotate Secrets Manager cert manually; restart tasks to pick up
  2. Validate ALB 443 and target 8443 health checks green

## Athena Staging Cleanup
- Symptom: S3 costs rising due to stale Athena staging objects
- Actions:
  1. Run cleanup script: `uv run python scripts/athena_staging_cleanup.py --bucket <bucket> --prefix <prefix> --age-days 7`
  2. Consider a scheduled job (EventBridge) if needed; monitor S3 cost after

## Tracing Steps
- Correlation ID: SQS messageId → Lambda logs (contextualize) → EMF metric dimensions → S3 object metadata → AppSync `x-correlation-id` header
- To trace a request:
  1. Start from CloudWatch Logs using correlationId
  2. Check EMF metrics (Activity/Inference) with CorrelationId dimension
  3. Retrieve S3 results object; inspect metadata for correlation-id
  4. Verify AppSync mutation logs for matching header
