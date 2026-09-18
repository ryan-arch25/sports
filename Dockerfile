# Dashboard image. Build context is the repo root.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    APP_USER=cfbedge

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# halfpoint.jso[n] is an optional committed half-point table. The bracket makes
# the pattern match nothing when the file is absent, and README.md alongside it
# guarantees the COPY always has at least one source.
COPY pyproject.toml README.md halfpoint.jso[n] ./
COPY cfb_edge ./cfb_edge
RUN pip install --no-cache-dir --no-deps .

# The application user. It owns /app, but not necessarily a volume mounted
# over DATA_DIR at run time -- that is the entrypoint's job.
RUN useradd --create-home --uid 10001 cfbedge \
    && mkdir -p "$DATA_DIR" \
    && chown -R cfbedge:cfbedge /app

EXPOSE 8000

# No USER line on purpose: the container starts as root so the entrypoint can
# take ownership of a freshly mounted volume (Railway mounts them owned by
# root), and then execs the command below as $APP_USER. The app itself never
# runs with privilege.
ENTRYPOINT ["python", "-m", "cfb_edge.entrypoint"]

# Railway injects PORT; the default keeps `docker run -p 8000:8000` working.
CMD ["sh", "-c", "exec uvicorn cfb_edge.web.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
