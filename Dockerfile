# syntax=docker/dockerfile:1
# ── Stage 1: Builder (dependencies + model download) ─────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download fastembed model if installed (baked into image, zero runtime downloads)
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed
RUN mkdir -p /app/.cache/fastembed && \
    python -c "from fastembed import TextEmbedding; print('Pre-downloading BAAI/bge-small-en-v1.5...'); e = TextEmbedding(model_name='BAAI/bge-small-en-v1.5'); list(e.embed(['warmup'])); print('FastEmbed model baked successfully')"

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user
RUN addgroup --system olake && adduser --system --ingroup olake olake

WORKDIR /app

# Copy Python site-packages from builder
COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy baked model cache from builder
COPY --from=builder --chown=olake:olake /app/.cache /app/.cache

# Copy application code
COPY app/ ./app/
COPY eval/ ./eval/
COPY scripts/ ./scripts/
COPY pyproject.toml .

# Copy prebuilt frontend bundle (pre-compiled and tracked in repository)
COPY frontend/dist/ ./frontend/dist/

# Create data directory (index volume mount point)
RUN mkdir -p /data/index && chown -R olake:olake /data /app

# Copy seed index data if present
COPY --chown=olake:olake data/ /data/

ENV DATA_DIR=/data \
    INDEX_DIR=/data/index \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_ENV=production

EXPOSE 8000

USER olake

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["python", "-m", "uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
