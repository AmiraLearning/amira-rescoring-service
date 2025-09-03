# Amira Letter Scoring Pipeline

A GPU-accelerated machine learning pipeline for scoring letter names and sounds using Wav2Vec2 models. Supports both Lambda-only CPU processing and distributed GPU inference with Triton servers.

## Features

- **Dual Architecture Support**:
  - **Lambda-only**: Pure CPU inference using AWS Lambda at scale
  - **Lambda + GPU Cluster**: CPU processing in Lambda with GPU inference via Triton servers
- **Audio Processing**: Optimized for real-time phonetic transcription
- **Auto-scaling Infrastructure**: Dynamic scaling based on queue depth and inference load
- **Comprehensive Monitoring**: CloudWatch metrics, alarms, and Slack notifications
- **Secure & Compliant**: Proper IAM roles, encryption, and input validation

## Architecture

### Mode 1: Lambda-only CPU Pipeline
```
Athena -> SQS -> Lambda (CPU Inference) -> S3 Results
```

### Mode 2: Lambda + GPU Cluster
```
Athena -> SQS -> Lambda (CPU Processing) -> Triton GPU Cluster -> S3 Results
                                              |
                                        ALB + ECS Fargate
```

## Prerequisites

- **Python 3.12+** with uv package manager
- **AWS CLI** configured with appropriate permissions
- **Node.js 18+** for CDK deployment
- **Docker** for containerized deployments
- **Rust** toolchain (for ASR aligner compilation)

## Quick Start

### 1. Setup Development Environment

```bash
# Clone repository
git clone <repository-url>
cd amira-letter-scoring

# Install Python dependencies
uv sync

# Install CDK dependencies
cd cdk && npm install && cd ..

# Build Rust extension (if needed)
cd my_asr_aligner && maturin develop --release && cd ..
```

### 2. Deploy Lambda-only Pipeline

```bash
# Deploy Lambda stack
cd cdk
cdk deploy AmiraLambdaParallelStack
```

### 3. Deploy Lambda + GPU Cluster

```bash
# Deploy both stacks
cdk deploy AmiraLambdaParallelStack --parameters UseTriton=false
cdk deploy AmiraLetterScoringStack --parameters UseTriton=true

# Update Lambda with Triton cluster URL
TRITON_URL=$(aws cloudformation describe-stacks --stack-name AmiraLetterScoringStack --query 'Stacks[0].Outputs[?OutputKey==`TritonClusterUrl`].OutputValue' --output text)
cdk deploy AmiraLambdaParallelStack --parameters TritonClusterUrl=$TRITON_URL
```

## Usage

### Running Local Pipeline

```bash
# Process a specific activity
python main.py run --activity-id A025D9AFCEB711EFB9CA0E57FBD5D8A1

# Run with configuration file
python main.py run --config-path config.yaml

# Use complete audio instead of phrase segments
python main.py run --activity-id <id> --use-complete-audio

# Clean up files after processing
python main.py run --cleanup
```

### Manual Job Enqueueing

```bash
# Trigger manual job enqueueing via Lambda
aws lambda invoke --function-name <manual-trigger-function> output.json
```

### Monitoring & Alerts

- **CloudWatch Dashboard**: Monitor GPU utilization, inference latency, and throughput
- **Slack Notifications**: Get alerts for job completion, failures, and system issues
- **Custom Metrics**: Track processing times, success rates, and queue depths

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `USE_TRITON` | Enable Triton GPU inference | `false` |
| `TRITON_URL` | Triton server endpoint | `http://localhost:8000` |
| `TRITON_MODEL` | Model name in Triton | `w2v2` |
| `MODEL_PATH` | Wav2Vec2 model path | `facebook/wav2vec2-base-960h` |
| `INCLUDE_CONFIDENCE` | Include confidence scores | `true` |
| `AWS_PROFILE` | AWS credential profile | `legacy` |
| `AWS_REGION` | AWS region | `us-east-2` |

### Pipeline Configuration

Create a `config.yaml` file:

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

## Container Images

### Application Container
```dockerfile
FROM 763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-inference:2.6.0-cpu-py312-ubuntu22.04-sagemaker
# Optimized for CPU inference with PyTorch
```

### Triton GPU Container
```dockerfile
FROM nvcr.io/nvidia/tritonserver:25.07-py3
# GPU-optimized for Triton inference serving
```

### Lambda Container
```dockerfile
FROM public.ecr.aws/lambda/python:3.12-arm64
# ARM64 optimized for AWS Lambda Graviton processors
```

## Performance

### Benchmarks

| Mode | Throughput | Latency | Cost (est.) |
|------|------------|---------|-------------|
| Lambda CPU | ~100 jobs/min | 15-30s | $0.05/job |
| Lambda + GPU | ~500 jobs/min | 5-10s | $0.08/job |

### Scaling Characteristics

- **Lambda**: Up to 1000 concurrent executions
- **GPU Cluster**: Auto-scales 0-10 instances based on load
- **Storage**: Partitioned S3 results by date and activity

## Monitoring

### Key Metrics

- **Job Completion Rate**: Success/failure ratios
- **Processing Time**: End-to-end latency metrics
- **GPU Utilization**: Memory and compute usage
- **Queue Depth**: SQS backlog monitoring
- **Error Rates**: Failed inference tracking

### Alarms & Notifications

- DLQ depth > 1 message
- Processing errors > 10 in 5 minutes
- Queue age > 15 minutes
- GPU utilization > 90%

## Development

### Project Structure

```
amira-letter-scoring/
├── src/pipeline/           # Core pipeline logic
│   ├── inference/         # ML inference engines
│   ├── audio_prep/        # Audio preprocessing
│   └── pipeline.py        # Main pipeline orchestration
├── utils/                 # Utility functions
├── lambda/               # Lambda function handlers
├── cdk/                  # Infrastructure as Code
├── infra/                # AWS client libraries
├── my_asr_aligner/       # Rust ASR alignment library
└── triton_models/        # Triton model configurations
```

### Running Tests

```bash
# Install dev dependencies
uv sync --group dev

# Run type checking
mypy src/

# Run linting
ruff check .

# Run formatting
ruff format .
```

### Dev Tooling

- Pre-commit hooks: install once, then hooks run on commit
  - `uv tool run pre-commit install` (or `pre-commit install` if installed globally)
  - Run all hooks manually: `pre-commit run --all-files`
- Hooks mirror CI exactly:
  - `ruff check .`
  - `ruff format --check .`
  - `mypy src/ utils/ infra/ lambda/ main.py`
- Linting & formatting: `ruff check .` and `ruff format .`
- Type checking: `mypy src/ utils/ infra/ lambda/ main.py`

### CI

- GitHub Actions workflow `.github/workflows/ci.yml` runs ruff and mypy on PRs/commits to main.
- Adjust ruff/mypy settings in `pyproject.toml` under `[tool.ruff]` and `[tool.mypy]`.

### Adding New Features

1. **Create feature branch**: `git checkout -b feature/new-feature`
2. **Implement changes**: Follow existing code patterns
3. **Add tests**: Ensure adequate test coverage
4. **Update documentation**: Update this README as needed
5. **Submit PR**: Include description of changes

## Troubleshooting

### Common Issues

**Model Loading Errors**
```bash
# Ensure model path is correct
export MODEL_PATH="facebook/wav2vec2-base-960h"
python -c "from transformers import Wav2Vec2ForCTC; Wav2Vec2ForCTC.from_pretrained('$MODEL_PATH')"
```

**Triton Connection Issues**
```bash
# Test Triton server connectivity
curl -v http://triton-server:8000/v2/health/ready
```

**Lambda Timeout Issues**
```bash
# Check CloudWatch logs
aws logs tail /aws/lambda/amira-parallel-processor --follow
```

**Permission Issues**
```bash
# Verify AWS credentials
aws sts get-caller-identity
aws s3 ls s3://your-results-bucket/
```

### Debug Mode

```bash
# Enable verbose logging
export ENABLE_FILE_LOG=1
python main.py run --activity-id <id>
tail -f pipeline_execution.log
```

## License

[Add your license information here]

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests and documentation
5. Submit a pull request

## Support

- **Issues**: Create GitHub issues for bugs and feature requests
- **Documentation**: Check the `docs/` directory for detailed guides
- **Slack**: Join #ml-pipeline-support for real-time help

---

> **Note**: This pipeline processes sensitive audio data. Ensure compliance with data privacy regulations and follow security best practices.
