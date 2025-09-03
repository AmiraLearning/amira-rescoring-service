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
