# Build context: repo root
FROM python:3.11-slim

WORKDIR /app

COPY services/web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip "wheel>=0.46.2" "jaraco.context>=6.1.0" && \
    pip install --no-cache-dir -r requirements.txt

COPY services/web/src/ ./src/
COPY shared/ ./shared/
COPY VERSION ./VERSION

# Build-time metadata injected by CI; exposed as env vars at runtime.
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_DATE=${BUILD_DATE}

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# start.py launches uvicorn on HTTP port 8080.
# TLS termination is handled by the Caddy reverse proxy container.
CMD ["python3", "src/start.py"]
