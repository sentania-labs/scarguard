# Build context: repo root
# docker build -f services/notifier/Dockerfile -t scarguard-notifier .
FROM python:3.11-slim@sha256:9358444059ed78e2975ada2c189f1c1a3144a5dab6f35bff8c981afb38946634

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

COPY services/notifier/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/notifier/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Create non-root user for defense in depth.
# Container starts as root; entrypoint fixes volume ownership then drops
# to scarguard via gosu.
RUN useradd -r -s /usr/sbin/nologin scarguard && \
    mkdir -p /data /config /var/lib/scarguard && \
    chown scarguard:scarguard /data /config /var/lib/scarguard

COPY services/notifier/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]
