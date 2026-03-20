FROM ubuntu:22.04
# Note: Using 22.04 to match JetPack 6.x userspace (Ubuntu 22.04 Jammy)
ENV DEBIAN_FRONTEND=noninteractive

# Base deps + Docker CLI (for issuing commands to host Docker via socket mount)
RUN apt-get update && apt-get install -y \
    build-essential \
    ca-certificates \
    curl \
    docker.io \
    git \
    jq \
    libicu70 \
    libssl3 \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    lsb-release \
 && rm -rf /var/lib/apt/lists/*

# --- Trust internal CA ---
# Place your sentania Lab Root CA cert alongside this Dockerfile
# IMPORTANT: Must be PEM-encoded with .crt extension
COPY ["sentania Lab Root 2.crt", "/usr/local/share/ca-certificates/sentania-lab-root-2.crt"]
RUN update-ca-certificates

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Non-root user for runner (match host user UID if needed)
ARG RUNNER_UID=1000
RUN useradd -m -u ${RUNNER_UID} runner \
 && usermod -aG docker runner
WORKDIR /home/runner

# GitHub Actions runner (ARM64)
# Check https://github.com/actions/runner/releases for latest version
ENV RUNNER_VERSION=2.330.0
RUN curl -L -O https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && tar xzf actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && rm actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && ./bin/installdependencies.sh

# Entrypoint script handles runner registration and startup
COPY --chmod=0755 entrypoint.sh /opt/entrypoint.sh

USER runner
ENTRYPOINT ["/opt/entrypoint.sh"]
