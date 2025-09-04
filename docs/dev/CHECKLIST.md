# Cloud Test Checklist

## Configure
- AppSync (optional): set `APPSYNC_URL`, `APPSYNC_API_KEY`.
- Triton (if GPU path): provide `TritonCertificateArn`; optional `TritonTargetCertSecretArn` (JSON with keys `cert`, `key`).
- Lambda stack: set results bucket, jobs queue, and `TritonClusterUrl` (if GPU path).

## Deploy
- CPU-only:
```bash
cd cdk
cdk deploy AmiraLambdaParallelStack
```
- With Triton:
```bash
cdk deploy AmiraLetterScoringStack --parameters UseTriton=true
TRITON_URL=$(aws cloudformation describe-stacks --stack-name AmiraLetterScoringStack \
  --query "Stacks[0].Outputs[?OutputKey=='TritonClusterUrl'].OutputValue" --output text)
cdk deploy AmiraLambdaParallelStack --parameters TritonClusterUrl=$TRITON_URL
```
- macOS only (run once):
```bash
bash fix.sh  # Adds FFmpeg RPATH to torchcodec dylibs
```

## Smoke Test (single activity)
- Invoke manual enqueue Lambda:
```bash
aws lambda invoke \
  --function-name <ManualTriggerFunctionName> \
  --payload '{"activity_ids":["<YOUR_ACTIVITY_ID>"]}' \
  /dev/stdout | cat
```
- Or send directly to SQS:
```bash
aws sqs send-message \
  --queue-url <TasksQueueUrl> \
  --message-body '{"activityId":"<YOUR_ACTIVITY_ID>"}'
```

## Observe
- Logs:
```bash
aws logs tail /aws/lambda/amira-parallel-processor --follow
```
- Dashboards: CloudWatch → `AmiraLambdaParallel` (and `AmiraGpuTriton` if GPU)
  - `Amira/Activity`: ActivityTotalMs (p95)
  - `Amira/Inference`: InferenceTotalMs (p95)
- S3 Result:
```bash
aws s3 ls s3://<results-bucket>/results/dt=$(date +%F)/activity_id=<YOUR_ACTIVITY_ID>/
aws s3 cp s3://<results-bucket>/results/dt=$(date +%F)/activity_id=<YOUR_ACTIVITY_ID>/results.json -
```

## TLS & Sidecar (GPU path)
- ALB listener 443 with ACM cert; target HTTPS:8443 via sidecar.
- Client enforcement: Triton URL must be `https://` (rejected if `http://`).

## DLQ & Redrive
- DLQ alarm in place. To redrive:
```bash
uv run python scripts/sqs_redrive_dlq.py --dlq-url <DLQ_URL> --dest-url <TasksQueueUrl> --max 500
```

## Rollback & Operations
- OIDC deploys: see `docs/OIDC_DEPLOYMENTS.md` (concurrency guards).
- Rollback & change review: see `docs/ROLLBACK_CHANGE_REVIEW.md`.
