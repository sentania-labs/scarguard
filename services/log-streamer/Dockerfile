# Build context: repo root
# docker build -f services/log-streamer/Dockerfile -t scarguard-log-streamer .
#
# Lightweight sidecar that tails Docker container logs and publishes them
# to Redis.  Replaces the Docker socket mount previously needed by the
# web container for the admin logs tab.
# v0.13.4: authenticated Docker Hub pull via DOCKER_HUB_USERNAME/_API_KEY
# org secrets (docker/login-action step in build.yml) - per-user quota
# (5000/day) instead of the anonymous-IP quota (~100/6h).
FROM docker.io/library/python:3.11-slim@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY services/log-streamer/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.lock && \
    pip uninstall -y pip setuptools wheel

COPY services/log-streamer/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# v1.14: drop to non-root. Docker socket access is mediated by
# docker-socket-proxy (over TCP), so this container no longer needs the
# root-uid privilege for the unix-socket file mode.
RUN addgroup --system --gid 1100 scarguard \
    && adduser --system --uid 1100 --ingroup scarguard scarguard
USER scarguard

CMD ["python3", "src/main.py"]
