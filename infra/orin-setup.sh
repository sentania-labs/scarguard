#!/usr/bin/env bash
# orin-setup.sh — Bootstrap Docker + NVIDIA Container Toolkit on Jetson Orin Nano
# Run as root or with sudo on JetPack 6.2.1 (L4T 36.4.7)
#
# Idempotent: detects existing installs and skips what's already done.
# Supports both Docker CE (docker-ce) and docker.io. Prefers whatever
# is already installed; defaults to Docker CE for fresh installs.
#
# Usage: sudo bash orin-setup.sh

set -euo pipefail

echo "=== ScarGuard Orin Host Setup ==="
echo ""

# --- Step 1: Install Docker (if not already present) ---
echo "[1/5] Checking Docker..."
if command -v docker &>/dev/null; then
    DOCKER_VERSION=$(docker --version)
    echo "  Docker already installed: ${DOCKER_VERSION}"

    # Check for Compose
    if docker compose version &>/dev/null; then
        echo "  Docker Compose: $(docker compose version --short)"
    elif command -v docker-compose &>/dev/null; then
        echo "  Docker Compose (standalone): $(docker-compose --version)"
    else
        echo "  WARNING: Docker Compose not found. Installing plugin..."
        apt-get update
        apt-get install -y docker-compose-plugin 2>/dev/null || apt-get install -y docker-compose-v2
    fi
else
    echo "  Docker not found. Installing Docker CE..."
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo "  Installed: $(docker --version)"
fi

echo ""

# --- Step 2: Install NVIDIA Container Toolkit (if not already present) ---
echo "[2/5] Checking NVIDIA Container Toolkit..."
if dpkg -l nvidia-container-toolkit &>/dev/null 2>&1; then
    NCT_VERSION=$(dpkg-query --showformat='${Version}' --show nvidia-container-toolkit 2>/dev/null)
    echo "  Already installed: nvidia-container-toolkit ${NCT_VERSION}"
else
    echo "  Installing NVIDIA Container Toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
      gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
      tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update
    apt-get install -y nvidia-container-toolkit
    echo "  Installed."
fi

echo ""

# --- Step 3: Configure NVIDIA as default Docker runtime ---
echo "[3/5] Configuring NVIDIA container runtime..."
if grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
    echo "  NVIDIA runtime already configured in daemon.json"
else
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
    echo "  Configured and Docker restarted."
fi

echo ""

# --- Step 4: Add current user to docker group ---
TARGET_USER="${SUDO_USER:-$USER}"
echo "[4/5] Checking docker group membership for ${TARGET_USER}..."
if id -nG "${TARGET_USER}" | grep -qw docker; then
    echo "  ${TARGET_USER} is already in the docker group."
else
    usermod -aG docker "${TARGET_USER}"
    echo "  Added ${TARGET_USER} to docker group."
    echo "  NOTE: Log out and back in for this to take effect."
fi

echo ""

# --- Step 5: Verify GPU access in container ---
echo "[5/5] Testing GPU access in a container..."
echo "  (This may pull an image on first run — could take a few minutes)"
echo ""
docker run --rm --runtime=nvidia --gpus all \
  dustynv/l4t-pytorch:r36.4.0 \
  python3 -c "import torch; print(f'  CUDA available: {torch.cuda.is_available()}'); print(f'  GPU: {torch.cuda.get_device_name(0)}')"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Log out and back in if docker group was just added"
echo "  2. Clone the ScarGuard repo (if not already done)"
echo "  3. Build and start the GitHub Actions runner:"
echo "     cd scarguard/infra"
echo "     docker compose -f docker-compose.runner.yml up -d --build"