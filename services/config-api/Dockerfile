# Build context: repo root
# docker build -f services/config-api/Dockerfile -t scarguard-config-api .
#
# v1.15: Config-write API service (optional, behind compose profile).
# Handles the 8 config-write POST endpoints when split from the web
# monolith.  When the "config-api" compose profile is not active this
# image is never built or started — the web service handles everything.
FROM docker.io/library/python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

COPY services/config-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/config-api/src/ ./src/
COPY shared/ ./shared/

ARG VERSION=local-dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV VERSION=${VERSION}
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_DATE=${BUILD_DATE}

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Non-root user — same uid/gid as web, detector, notifier, deterrent.
# Entrypoint chowns volume mounts then drops to this user via gosu.
RUN useradd -r -s /usr/sbin/nologin scarguard && \
    mkdir -p /data /config && \
    chown scarguard:scarguard /data /config

COPY services/config-api/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "src/start.py"]
