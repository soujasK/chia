# ==============================================================================
# NEURO-CIRCT: Autonomous Hardware Compiler Bug Repair
# Reproducibility Docker Container (CHIA Hackathon 2026 - Track §5.5)
# ==============================================================================
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install baseline runtime tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /workspace

# Copy entire repository
COPY . /workspace

# Install python dependencies for CHIA and test suite
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    pytest>=8.0 \
    lit>=18.0 \
    requests \
    pyyaml \
    psutil \
    click \
    jinja2

# Set python path
ENV PYTHONPATH=/workspace:/workspace/examples/circt_issue_solver

# Default command: 1-Click Verification & Defect Reproduction
WORKDIR /workspace/examples/circt_issue_solver
CMD ["bash", "reproduce_results.sh"]
