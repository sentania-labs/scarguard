#!/usr/bin/env bash
# orin-setup.sh — Bootstrap Docker + NVIDIA Container Toolkit on Jetson Orin Nano
# Run as root or with sudo on a fresh JetPack 6.2.1 (L4T 36.4.7) install
#
# Usage: sudo bash orin-setup.sh

set -euo pipefail

echo "=== ScarGuard Orin Host Setup ==="
echo ""

# --- Step 1: Install Docker ---
echo "[1/5] Installing Docker..."
apt-get update
apt-get install -y docker.io docker-compose-v2
systemctl enable docker
systemctl start docker

# --- Step 2: Install NVIDIA Container Toolkit ---
echo "[2/5] Installing NVIDIA Container Toolkit..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit

# --- Step 3: Configure NVIDIA as default Docker runtime ---
echo "[3/5] Configuring NVIDIA container runtime..."
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# --- Step 4: Add current user to docker group ---
echo "[4/5] Adding ${SUDO_USER:-$USER} to docker group..."
usermod -aG docker "${SUDO_USER:-$USER}"

# --- Step 5: Verify GPU access in container ---
echo "[5/5] Testing GPU access in a container..."
echo "  (This will pull an NVIDIA base image — may take a few minutes on first run)"
docker run --rm --runtime=nvidia --gpus all \
  nvcr.io/nvidia/l4t-pytorch:r36.4.0-pth2.5-py3 \
  python3 -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0)}')"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "IMPORTANT: Log out and back in for docker group membership to take effect."
echo ""
echo "Next steps:"
echo "  1. Log out and back in (or run: newgrp docker)"
echo "  2. Clone the ScarGuard repo"
echo "  3. Build and start the GitHub Actions runner:"
echo "     cd scarguard/infra"
echo "     docker compose -f docker-compose.runner.yml up -d --build"
