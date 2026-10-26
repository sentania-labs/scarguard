FROM docker.io/library/python:3.11-slim@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

WORKDIR /app
# APT_CACHE_BUST: CI passes a fresh value per run so the buildx GHA cache
# never serves this layer with pre-upgrade packages (trips the Trivy gate).
ARG APT_CACHE_BUST=dev
RUN echo "apt cache bust: ${APT_CACHE_BUST}" && \
    apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*
COPY services/off-watchdog/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.lock && \
    pip uninstall -y pip setuptools wheel
COPY services/off-watchdog/src/ ./src/
COPY shared/ ./shared/
ENV PYTHONPATH=/app/src:/app/shared PYTHONUNBUFFERED=1
RUN useradd -r -s /usr/sbin/nologin scarguard && mkdir -p /config /data && \
    chown scarguard:scarguard /config /data
COPY services/off-watchdog/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]
