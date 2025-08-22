FROM python:3.12-slim AS builder

# Install system dependencies and Rust in a single layer
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"
WORKDIR /app

# Copy only dependency files first to leverage Docker cache
COPY pyproject.toml uv.lock ./
COPY my_asr_aligner/ ./my_asr_aligner/

# Install uv and dependencies
RUN pip install uv && uv sync --frozen

# Build Rust extension
WORKDIR /app/my_asr_aligner
RUN maturin develop --release
WORKDIR /app

# Create a smaller runtime image
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages and built extension from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /app/my_asr_aligner/ /app/my_asr_aligner/

# Copy application code
COPY src/ ./src/
COPY utils/ ./utils/
COPY infra/ ./infra/
COPY main.py ./

ENV PYTHONPATH=/app

CMD ["python", "main.py", "--help"]