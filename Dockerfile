# Dashboard image. Build context is the repo root.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY pyproject.toml README.md ./
COPY cfb_edge ./cfb_edge
RUN pip install --no-cache-dir --no-deps .

# Run as a non-root user that owns the writable data directory.
RUN useradd --create-home --uid 10001 cfbedge \
    && mkdir -p /app/data \
    && chown -R cfbedge:cfbedge /app
USER cfbedge

EXPOSE 8000

# Railway injects PORT; the default keeps `docker run -p 8000:8000` working.
CMD ["sh", "-c", "exec uvicorn cfb_edge.web.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
