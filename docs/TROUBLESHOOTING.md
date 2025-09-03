# Troubleshooting

## Model Loading Errors
```bash
export MODEL_PATH="facebook/wav2vec2-base-960h"
python -c "from transformers import Wav2Vec2ForCTC; Wav2Vec2ForCTC.from_pretrained('$MODEL_PATH')"
```

## Triton Connection Issues
```bash
curl -vk https://<alb-dns>/v2/health/ready
```

## Lambda Timeout
```bash
aws logs tail /aws/lambda/amira-parallel-processor --follow
```

## Permission Issues
```bash
aws sts get-caller-identity
aws s3 ls s3://<results-bucket>/
```

## Debug Mode
```bash
export ENABLE_FILE_LOG=1
python main.py run --activity-id <id>
tail -f pipeline_execution.log
```
