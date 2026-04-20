# Build context: repo root
# v0.13.3: pull via Google's public Docker Hub mirror (mirror.gcr.io) to
# dodge Docker Hub's anonymous rate limit on GHA shared-runner IPs.
# Same digest, no quota.
FROM mirror.gcr.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

COPY services/web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/web/src/ ./src/
COPY shared/ ./shared/
# Build-time metadata injected by CI; exposed as env vars at runtime.
ARG VERSION=local-dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV VERSION=${VERSION}
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_DATE=${BUILD_DATE}

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Create non-root user for defense in depth.
# Container starts as root; entrypoint fixes volume ownership then drops
# to scarguard via gosu.
RUN useradd -r -s /usr/sbin/nologin scarguard && \
    mkdir -p /data /config /models && \
    chown scarguard:scarguard /data /config /models

COPY services/web/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
# start.py launches uvicorn on HTTP port 8080.
# TLS termination is handled by the Caddy reverse proxy container.
CMD ["python3", "src/start.py"]
