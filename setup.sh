#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  ScarGuard — First-Run Setup
#
#  This script prepares a Jetson Orin Nano to run ScarGuard.
#  It is safe to run more than once (idempotent).
#
#  Usage:  bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
CYAN=$(tput setaf 6 2>/dev/null || true)

info()    { echo "${GREEN}[✓]${RESET} $*"; }
warn()    { echo "${YELLOW}[!]${RESET} $*"; }
error()   { echo "${RED}[✗]${RESET} $*" >&2; }
step()    { echo; echo "${BOLD}${CYAN}── $* ──${RESET}"; }
ask()     { printf "%s" "${BOLD}${YELLOW}[?]${RESET} $* "; }

# Prompt yes/no, default to $2 ("y" or "n"). Returns 0 for yes, 1 for no.
confirm() {
    local prompt="$1"
    local default="${2:-y}"
    local hint
    if [[ "$default" == "y" ]]; then
        hint="[Y/n]"
    else
        hint="[y/N]"
    fi
    ask "$prompt $hint: "
    local reply
    read -r reply </dev/tty
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

# Script must run from the repo root (where docker-compose.yml lives).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo
echo "${BOLD}╔════════════════════════════════════════╗${RESET}"
echo "${BOLD}║       ScarGuard — First-Run Setup      ║${RESET}"
echo "${BOLD}╚════════════════════════════════════════╝${RESET}"
echo "  Running from: $REPO_ROOT"

# ── Step 1: Architecture check ───────────────────────────────────────────────
step "Checking platform"

ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
    warn "This machine reports arch=${ARCH}, not aarch64."
    warn "ScarGuard is designed for Jetson Orin Nano (ARM64)."
    if ! confirm "Continue anyway (e.g. for testing on x86)?"; then
        echo "Exiting. Run this script on your Jetson Orin Nano."
        exit 1
    fi
else
    info "Platform: ARM64 (aarch64) — looks like a Jetson."
fi

# ── Step 2: Docker check ──────────────────────────────────────────────────────
step "Checking Docker"

NEEDS_SETUP=false

if ! command -v docker &>/dev/null; then
    error "docker is not installed."
    NEEDS_SETUP=true
elif ! docker compose version &>/dev/null; then
    error "docker compose plugin is not available (docker compose v2 required)."
    NEEDS_SETUP=true
else
    DOCKER_VER=$(docker --version)
    COMPOSE_VER=$(docker compose version --short)
    info "Docker: $DOCKER_VER"
    info "Compose: $COMPOSE_VER"
fi

# ── Step 3: NVIDIA container runtime check ────────────────────────────────────
step "Checking NVIDIA container runtime"

NVIDIA_OK=false
if docker info --format '{{.Runtimes}}' 2>/dev/null | grep -q nvidia; then
    NVIDIA_OK=true
    info "NVIDIA container runtime: found"
else
    error "NVIDIA container runtime is not configured in Docker."
    NEEDS_SETUP=true
fi

# Offer to run the host setup script if prerequisites are missing.
if [[ "$NEEDS_SETUP" == "true" ]]; then
    echo
    warn "Some prerequisites are missing."
    if [[ -f "infra/orin-setup.sh" ]]; then
        if confirm "Run infra/orin-setup.sh now to install Docker + NVIDIA runtime?"; then
            echo
            sudo bash infra/orin-setup.sh
            echo
            # Re-check Docker after setup.
            if ! command -v docker &>/dev/null || ! docker compose version &>/dev/null; then
                error "Setup did not resolve Docker issues. Please check the output above."
                exit 1
            fi
            if ! docker info --format '{{.Runtimes}}' 2>/dev/null | grep -q nvidia; then
                warn "NVIDIA runtime still not detected. You may need to log out and back in,"
                warn "then re-run this script."
                warn "Continuing anyway — non-GPU services will still start."
            else
                info "NVIDIA container runtime: found"
                NVIDIA_OK=true
            fi
        else
            echo
            error "Cannot continue without Docker and the NVIDIA container runtime."
            echo  "Install manually with:  sudo bash infra/orin-setup.sh"
            exit 1
        fi
    else
        error "infra/orin-setup.sh not found. Ensure you are in the repo root."
        exit 1
    fi
fi

# ── Step 4: Create .env ───────────────────────────────────────────────────────
step "Setting up environment (.env)"

if [[ -f ".env" ]]; then
    info ".env already exists — loading."
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    # Prompt for HTTP port
    ask "HTTP port? (press Enter for default 80): "
    read -r HTTP_PORT_INPUT </dev/tty
    HTTP_PORT_VALUE="${HTTP_PORT_INPUT:-80}"

    # Validate it's a number in a reasonable range
    if ! [[ "$HTTP_PORT_VALUE" =~ ^[0-9]+$ ]] || \
       [[ "$HTTP_PORT_VALUE" -lt 1 ]] || \
       [[ "$HTTP_PORT_VALUE" -gt 65535 ]]; then
        warn "Invalid port '$HTTP_PORT_VALUE' — using 80."
        HTTP_PORT_VALUE=80
    fi

    cp .env.example .env
    if [[ "$HTTP_PORT_VALUE" != "80" ]]; then
        sed -i "s/^HTTP_PORT=.*/HTTP_PORT=${HTTP_PORT_VALUE}/" .env
    fi

    info "Created .env (HTTP_PORT=${HTTP_PORT_VALUE})"
fi

# Read HTTP_PORT for use in the final message
HTTP_PORT_FINAL=$(grep -E '^HTTP_PORT=' .env | cut -d= -f2 || echo "80")

# ── Step 5: Initialize config volume ─────────────────────────────────────────
step "Setting up configuration (named volume: scarguard-config)"

CONFIG_IS_NEW=false

# Check whether scarguard.yml already exists in the config volume.
if docker run --rm -v scarguard-config:/config alpine:3.20 test -f /config/scarguard.yml 2>/dev/null; then
    info "scarguard.yml already exists in config volume — skipping."
else
    docker run --rm \
        -v scarguard-config:/config \
        -v "${REPO_ROOT}/config:/src:ro" \
        alpine:3.20 sh -c 'cp /src/scarguard.example.yml /config/scarguard.yml && mkdir -p /config/certs'
    info "Created scarguard.yml in config volume from example."
    CONFIG_IS_NEW=true
fi

# Ensure certs subdirectory exists in the config volume.
docker run --rm -v scarguard-config:/config alpine:3.20 mkdir -p /config/certs 2>/dev/null || true

# ── Step 6: Ensure data volume directories ───────────────────────────────────
step "Preparing data volume (named volume: scarguard-data)"

docker run --rm -v scarguard-data:/data alpine:3.20 mkdir -p /data/snapshots
info "scarguard-data volume ready (snapshots directory created)"

# ── Step 7: TLS setup ───────────────────────────────────────────────────────
step "TLS configuration"

echo "  How will you access ScarGuard?"
echo "    1) LAN only (HTTP, no TLS — default)"
echo "    2) Internet with automatic HTTPS (Let's Encrypt)"
echo "    3) Own certificates (manual TLS)"
echo
ask "Choice [1]: "
read -r TLS_CHOICE </dev/tty
TLS_CHOICE="${TLS_CHOICE:-1}"

case "$TLS_CHOICE" in
    2)
        ask "Domain name (e.g. scarguard.example.com): "
        read -r TLS_DOMAIN </dev/tty
        if [[ -n "$TLS_DOMAIN" ]]; then
            # Update tls section in config volume
            docker run --rm -v scarguard-config:/config alpine:3.20 \
                sh -c "sed -i 's/mode: \"off\"/mode: \"auto\"/' /config/scarguard.yml && \
                       sed -i 's/domain: \"\"/domain: \"${TLS_DOMAIN}\"/' /config/scarguard.yml"
            info "TLS mode set to auto (Let's Encrypt) with domain: ${TLS_DOMAIN}"
            warn "Ports 80 and 443 must be reachable from the internet for ACME challenges."
        else
            warn "No domain provided — keeping TLS off. Change in Settings > TLS later."
        fi
        ;;
    3)
        info "TLS mode: manual. Place cert.pem and key.pem in the config volume's certs/ directory."
        docker run --rm -v scarguard-config:/config alpine:3.20 \
            sh -c "sed -i 's/mode: \"off\"/mode: \"manual\"/' /config/scarguard.yml"
        info "Set TLS mode to manual in scarguard.yml."
        warn "HTTPS will activate once cert and key files are present at the configured paths."
        ;;
    *)
        info "TLS disabled (HTTP only). You can enable it later in Settings > TLS."
        ;;
esac

# ── Step 8: Model check / starter model download ──────────────────────────────
step "Checking for YOLO model"

# Check for model files in the models volume.
MODEL_FILES=$(docker run --rm -v scarguard-models:/models alpine:3.20 sh -c \
    'ls /models/*.pt /models/*.engine 2>/dev/null || true')

if [[ -n "$MODEL_FILES" ]]; then
    info "Model file(s) found in models volume:"
    while IFS= read -r f; do
        info "  $(basename "$f")"
    done <<< "$MODEL_FILES"
else
    warn "No model file found in models volume."
    echo
    echo "  ScarGuard needs a YOLO model to detect wildlife."
    echo
    echo "  Option A — Starter model (recommended for first-time setup):"
    echo "    Downloads yolov8n.pt from Ultralytics (~6 MB)."
    echo "    Detects generic ${BOLD}birds${RESET} (class 'bird' from the COCO dataset)."
    echo "    ${YELLOW}It will NOT distinguish herons from sparrows.${RESET}"
    echo "    Good for verifying the pipeline works before training a custom model."
    echo
    echo "  Option B — Custom model:"
    echo "    Upload your own YOLO model via the web UI Models page after setup."
    echo


    if confirm "Download the starter model (yolov8n.pt) now?" "y"; then
        STARTER_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
        STARTER_DOWNLOADED=false

        _tmp_model=$(mktemp)
        if command -v curl &>/dev/null; then
            echo "  Downloading yolov8n.pt..."
            if curl -fL --progress-bar "$STARTER_URL" -o "$_tmp_model"; then
                STARTER_DOWNLOADED=true
            fi
        elif command -v wget &>/dev/null; then
            echo "  Downloading yolov8n.pt..."
            if wget -q --show-progress "$STARTER_URL" -O "$_tmp_model"; then
                STARTER_DOWNLOADED=true
            fi
        else
            error "Neither curl nor wget is available. Download the starter model manually"
            error "and upload it via the web UI Models page."
        fi

        if [[ "$STARTER_DOWNLOADED" == "true" ]]; then
            docker run --rm \
                -v scarguard-models:/models \
                -v "${_tmp_model}:/src/yolov8n.pt:ro" \
                alpine:3.20 cp /src/yolov8n.pt /models/yolov8n.pt
            info "Downloaded: yolov8n.pt (stored in models volume)"
            if [[ "$CONFIG_IS_NEW" == "true" ]]; then
                info "config/scarguard.yml is pre-configured for the starter model."
            fi
        else
            warn "Download failed. Check your internet connection."
            warn "You can upload a model via the web UI Models page after setup."
        fi
        rm -f "$_tmp_model"
    else
        warn "Skipping starter model download."
        warn "Upload your model via the web UI Models page before starting."
    fi
fi

# ── Step 9: Pull images ───────────────────────────────────────────────────────
step "Pulling Docker images from GHCR"

echo "  This downloads pre-built images — no compilation required."
echo

# Source .env so compose picks up the variables
set -a
# shellcheck disable=SC1091
source .env
set +a

if docker compose pull; then
    info "Images pulled successfully."
else
    error "docker compose pull failed."
    warn "This may be a network or authentication issue (e.g. private GHCR registry)."
    echo
    if [[ -t 1 ]] && confirm "Pull failed. Build images locally from source instead?" "n"; then
        echo "  Running docker compose build (this may take several minutes)..."
        if docker compose build; then
            info "Images built successfully from source."
        else
            error "docker compose build also failed. Check the output above."
            warn "You can retry manually:  docker compose build"
        fi
    else
        warn "Skipping image build. Run one of these before starting:"
        warn "  docker compose pull    (once registry access is resolved)"
        warn "  docker compose build   (to build from source)"
    fi
fi

# ── Step 10: Create initial admin account ─────────────────────────────────────
echo
step "Creating initial admin account"
echo "  If you skip this, visit the web UI on first launch to create an account."
echo

WEB_IMAGE=$(docker compose config --format json 2>/dev/null | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d['services']['web']['image'])" 2>/dev/null || echo "")

if [[ -z "$WEB_IMAGE" ]]; then
    warn "Could not determine web image name. Skipping admin account creation."
    warn "Create your admin account via the web UI on first launch."
else
    if [[ -t 0 ]] && confirm "Create admin account now?" "y"; then
        read -rp "  Admin username: " ADMIN_USER
        while [[ -z "$ADMIN_USER" ]]; do
            warn "Username cannot be empty."
            read -rp "  Admin username: " ADMIN_USER
        done
        while true; do
            read -rsp "  Admin password (min 8 characters): " ADMIN_PASS
            echo
            if [[ ${#ADMIN_PASS} -ge 8 ]]; then
                break
            fi
            warn "Password must be at least 8 characters."
        done

        if docker run --rm \
                -e AUTH_DB_PATH=/data/auth.db \
                -v scarguard-data:/data \
                "$WEB_IMAGE" \
                python /app/src/auth.py create-admin "$ADMIN_USER" "$ADMIN_PASS"; then
            info "Admin account '${ADMIN_USER}' created."
        else
            warn "Account creation failed. Create it via the web UI on first launch."
        fi
    else
        info "Skipped. Create your admin account via the web UI on first launch."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo "${BOLD}${GREEN}║  Setup complete!                                             ║${RESET}"
echo "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo

echo "${BOLD}Next steps:${RESET}"
echo
echo "  1. ${BOLD}Configure your cameras:${RESET}"
echo "       Start the stack and use the web UI config editor to set your RTSP URLs."
echo "       Or edit the config directly via a temporary container:"
echo "         docker run --rm -it -v scarguard-config:/config alpine:3.20 vi /config/scarguard.yml"
echo

if [[ -z "$MODEL_FILES" ]]; then
    echo "  2. ${BOLD}Add a YOLO model${RESET} (if you skipped the starter download):"
    echo "       Upload via the web UI Models page after starting the stack."
    echo
    echo "  3. ${BOLD}Start ScarGuard:${RESET}"
else
    echo "  2. ${BOLD}Start ScarGuard:${RESET}"
fi

echo "       docker compose up -d"
echo

echo "  $([[ -z "$MODEL_FILES" ]] && echo 4 || echo 3). ${BOLD}Open the web UI:${RESET}"

# Determine likely IP for the Orin
ORIN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' || echo "YOUR_ORIN_IP")
if [[ "$HTTP_PORT_FINAL" == "80" ]]; then
    echo "       http://${ORIN_IP}"
else
    echo "       http://${ORIN_IP}:${HTTP_PORT_FINAL}"
fi
echo

if [[ "$NVIDIA_OK" == "false" ]]; then
    echo "${YELLOW}Reminder:${RESET} NVIDIA container runtime was not detected."
    echo "  The detector service (GPU inference) will fail to start."
    echo "  Log out and back in, then run:  docker compose up -d"
    echo
fi

if [[ "$CONFIG_IS_NEW" == "true" ]]; then
    echo "${YELLOW}Important:${RESET} scarguard.yml was created from the example in the config volume."
    echo "  Update your RTSP camera URLs before starting — the system will not"
    echo "  detect anything until real camera streams are configured."
    echo
fi

echo "  To view logs:     docker compose logs -f"
echo "  To stop:          docker compose down"
echo "  To update images: docker compose pull && docker compose up -d"
echo
