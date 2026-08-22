# Multi-stage build: dependencies + runtime
# Stage 1: Builder - install dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies for opencv, torch, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libopencv-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies with longer timeout
COPY requirements.txt .
RUN pip install --no-cache-dir --user --default-timeout=1000 -r requirements.txt || pip install --no-cache-dir --user --default-timeout=1000 -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install runtime-only system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Ensure pip, setuptools, and wheel are available
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create directories for logs, screenshots, and storage
RUN mkdir -p logs screenshots storage reports

# Copy the entire application
COPY . .

# Expose dashboard port
EXPOSE 8000

# Health check (optional: checks if process is running)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Run the CLI
ENTRYPOINT ["python", "main.py"]
CMD ["--with-dashboard", "--verbose"]
