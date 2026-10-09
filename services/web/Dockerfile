# Build context: repo root
# v0.13.4: pull from Docker Hub with authenticated login (DOCKER_HUB_USERNAME /
# DOCKER_HUB_API_KEY org secrets) so we have the per-user quota (5000/day)
# instead of the anonymous-IP quota (~100/6h) that kept hitting us on GHA
# shared runners.
FROM docker.io/library/python:3.11-slim@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY services/web/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.lock && \
    pip uninstall -y pip setuptools wheel

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
