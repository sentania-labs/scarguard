# Narrow lifecycle boundary for hard detector GPU isolation.
FROM docker.io/library/python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

# Apply Debian security updates on top of the digest-pinned base, matching
# every other Python service Dockerfile here.  This file was the only one
# missing the step, so the image shipped the base's unpatched OS packages and
# the Trivy gate failed on them (util-linux CVE-2026-53612/53613/53614/53615).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

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
