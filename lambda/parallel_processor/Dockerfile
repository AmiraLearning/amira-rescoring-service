# Optimized PyTorch Lambda with uv
# ARM64 Graviton with torch.compile and quantization

FROM public.ecr.aws/lambda/python:3.12-arm64

# Install uv for fast dependency management and Rust toolchain for aligner
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Rust toolchain for building the aligner
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    . ~/.cargo/env && \
    echo 'source ~/.cargo/env' >> ~/.bashrc

# Optimization environment variables
ENV PYTHONOPTIMIZE=2
# Allow bytecode to speed imports
ENV UV_SYSTEM_PYTHON=1
ENV UV_CACHE_DIR=/tmp/.uv-cache

# ARM64 Graviton CPU optimizations
ENV OMP_NUM_THREADS=6
ENV TORCH_NUM_THREADS=6
ENV TORCH_NUM_INTEROP_THREADS=2

# ARM64-specific optimizations for Graviton2/3
ENV OMP_PROC_BIND=true
ENV OMP_PLACES=cores
ENV MALLOC_ARENA_MAX=2

# Memory management optimizations for ARM64
ENV PYTHONMALLOC=malloc
ENV MALLOC_MMAP_THRESHOLD_=131072
ENV MALLOC_TRIM_THRESHOLD_=131072

# Model cache paths
ENV TRANSFORMERS_CACHE=/opt/models/transformers
ENV HF_HOME=/opt/models/huggingface
ENV TORCH_HOME=/opt/models/torch

# Create cache directories
RUN mkdir -p /opt/models/transformers /opt/models/huggingface /opt/models/torch /tmp/.uv-cache

# Copy project files for uv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY utils/ ./utils/
COPY infra/ ./infra/
COPY my_asr_aligner/ ./my_asr_aligner/

# Install dependencies with uv using lockfile for reproducible builds
RUN uv sync --frozen --system

# Build Rust aligner extension with maturin
RUN . ~/.cargo/env && \
    uv pip install --system maturin && \
    maturin develop

# Pre-download Wav2Vec2 model for fast startup
ENV MODEL_PATH=facebook/wav2vec2-base-960h
RUN python -c "
import torch
from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

print('Pre-caching Wav2Vec2 model for fast startup...')

# Load and save with float16 for faster loading
processor = Wav2Vec2Processor.from_pretrained('${MODEL_PATH}')
model = Wav2Vec2ForCTC.from_pretrained('${MODEL_PATH}', torch_dtype=torch.float16)

# Set eval mode (no other optimizations needed for single-use)
model.eval()

# Save to container cache
model.save_pretrained('/opt/models/wav2vec2-optimized')
processor.save_pretrained('/opt/models/wav2vec2-optimized')

print('Model pre-cached for fast startup')
"

# Pre-compile all Python bytecode for faster imports
RUN python -m compileall /opt/python /var/runtime

# Copy Lambda handler
COPY lambda/parallel_processor/index.py ${LAMBDA_TASK_ROOT}/index.py

# Validate everything works
RUN python -c "
import torch, transformers, numpy, boto3
print('All imports validated')
print(f'PyTorch version: {torch.__version__}')
print(f'Transformers version: {transformers.__version__}')
print('Ready for inference!')
"

CMD ["index.lambda_handler"]
