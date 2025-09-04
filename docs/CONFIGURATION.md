# Configuration

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `USE_TRITON` | Enable Triton GPU inference | `false` |
| `TRITON_URL` | Triton server endpoint | `https://<alb-dns>` |
| `TRITON_MODEL` | Model name in Triton | `w2v2` |
| `MODEL_PATH` | Wav2Vec2 model path | `facebook/wav2vec2-base-960h` |
| `INCLUDE_CONFIDENCE` | Include confidence scores | `true` |
| `AWS_PROFILE` | AWS credential profile | `legacy` |
| `AWS_REGION` | AWS region | `us-east-2` |
| `PIPELINE_MAX_CONCURRENCY` | Max concurrent activities in pipeline | `4` |
| `PROCESSING_START_TIME` | ISO8601 start datetime for processing window | — |
| `PROCESSING_END_TIME` | ISO8601 end datetime for processing window | — |
| `PROCESSING_HOURS_AGO` | Rolling window size in hours (if start/end omitted) | — |

### S3 Client

| Variable | Description | Default |
|----------|-------------|---------|
| `S3_CLIENT_POOL_SIZE` | Number of pooled S3 clients | `20` |
| `S3_MAX_CONCURRENT_DOWNLOADS` | Max concurrent downloads | `64` |
| `S3_MAX_CONCURRENT_UPLOADS` | Max concurrent uploads | `32` |
| `S3_MAX_CONCURRENT_OPERATIONS` | Max concurrent generic ops (HEAD/LIST) | `128` |
| `S3_MAX_CONNECTIONS_PER_HOST` | HTTP connection pool per host | `64` |
| `S3_MAX_CONNECTIONS` | Total HTTP connections | `128` |
| `S3_CONNECTION_TIMEOUT` | Connect timeout (sec) | `30` |
| `S3_READ_TIMEOUT` | Read timeout (sec) | `180` |
| `S3_RETRY_MAX` / `S3_MAX_RETRIES` | Max retry attempts | `5` |
| `S3_RETRY_BASE` / `S3_RETRY_BACKOFF_BASE` | Backoff base (sec) | `0.1` |
| `S3_RETRY_MAX_BACKOFF` / `S3_RETRY_BACKOFF_MAX` | Backoff cap (sec) | `60` |
| `S3_USE_HEAD_FOR_PROGRESS` | Use HEAD to set download totals | `false` |

## Pipeline Configuration (YAML)

```yaml
metadata:
  activity_id: "A025D9AFCEB711EFB9CA0E57FBD5D8A1"
  limit: 10

audio:
  use_complete_audio: true
  padded_seconds: 3

w2v2:
  model_path: "facebook/wav2vec2-base-960h"
  include_confidence: true
  use_triton: false

aws:
  region: "us-east-2"
  aws_profile: "legacy"
```
