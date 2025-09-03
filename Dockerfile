# Use AWS Deep Learning Container with PyTorch pre-installed
FROM 763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-inference:2.6.0-cpu-py312-ubuntu22.04-sagemaker AS builder

# Install Rust for building the ASR aligner
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"
WORKDIR /app

# Copy dependency files and build Rust extension
COPY pyproject.toml uv.lock ./
COPY my_asr_aligner/ ./my_asr_aligner/

# Install uv and Python dependencies
RUN pip install uv && uv sync --frozen

# Build Rust extension
WORKDIR /app/my_asr_aligner
RUN maturin develop --release
WORKDIR /app

# Final optimized runtime image
FROM 763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-inference:2.6.0-cpu-py312-ubuntu22.04-sagemaker

WORKDIR /app

# Copy Python packages and Rust extension from builder
COPY --from=builder /opt/conda/lib/python3.12/site-packages/ /opt/conda/lib/python3.12/site-packages/
COPY --from=builder /app/my_asr_aligner/ /app/my_asr_aligner/

# Copy application code
COPY src/ ./src/
COPY utils/ ./utils/
COPY infra/ ./infra/
COPY main.py ./
COPY data/ ./data/

# Model will be mounted at runtime via volume
ENV MODEL_PATH=/models/wav2vec2-ft-large-eng-phoneme-amirabet_2025-04-24
ENV PYTHONPATH=/app

# Set up model mount point
RUN mkdir -p /models

CMD ["python", "main.py", "--help"]
