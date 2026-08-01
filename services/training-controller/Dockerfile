# Narrow lifecycle boundary for hard detector GPU isolation.
FROM docker.io/library/python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

# The base image's setuptools/wheel carry fixable HIGH CVEs (Trivy-gated),
# so upgrade the build tooling before installing the controller's pinned YAML
# parser.
COPY services/training-controller/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY services/training-controller/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/main.py"]
