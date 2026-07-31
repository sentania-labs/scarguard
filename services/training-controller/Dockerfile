# Narrow lifecycle boundary for hard detector GPU isolation.
FROM docker.io/library/python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

# No third-party deps, but the base image's setuptools/wheel carry fixable
# HIGH CVEs (Trivy-gated): upgrade them like every other slim-based service.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY services/training-controller/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/main.py"]
