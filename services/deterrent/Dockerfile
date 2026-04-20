# Build context: repo root
# docker build -f services/deterrent/Dockerfile -t scarguard-deterrent .
# v0.13.3: pull via Google's public Docker Hub mirror to dodge anon rate
# limits on GHA shared-runner IPs.
FROM mirror.gcr.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

# Upgrade base packages to pick up security fixes not yet in the slim image,
# then install gosu for entrypoint user-switching.
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

COPY services/deterrent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/deterrent/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Create non-root user for defense in depth.
# Container starts as root; entrypoint fixes volume ownership then drops
# to scarguard via gosu.
RUN useradd -r -s /usr/sbin/nologin scarguard && \
    mkdir -p /data /config && \
    chown scarguard:scarguard /data /config

COPY services/deterrent/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]
