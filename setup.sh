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

# Detect upgrade vs fresh install early (before banner)
IS_UPGRADE=false
if [[ -f ".env" ]]; then
    IS_UPGRADE=true
fi

echo
if [[ "$IS_UPGRADE" == "true" ]]; then
    echo "${BOLD}╔════════════════════════════════════════╗${RESET}"
    echo "${BOLD}║       ScarGuard — Upgrade Check        ║${RESET}"
    echo "${BOLD}╚════════════════════════════════════════╝${RESET}"
else
    echo "${BOLD}╔════════════════════════════════════════╗${RESET}"
    echo "${BOLD}║       ScarGuard — First-Run Setup      ║${RESET}"
    echo "${BOLD}╚════════════════════════════════════════╝${RESET}"
fi
echo "  Running from: $REPO_ROOT"

# ── Step 1: Platform detection ───────────────────────────────────────────────
step "Detecting platform"

ARCH=$(uname -m)
case "$ARCH" in
    aarch64)
        if [[ -f /etc/nv_tegra_release ]] || grep -q "NVIDIA" /proc/device-tree/compatible 2>/dev/null; then
            PLATFORM="jetson"
            DETECTOR_IMG_DEFAULT="ghcr.io/sentania-labs/scarguard-detector"
        else
            error "Generic ARM64 detected. ScarGuard requires a Jetson (ARM64+GPU) or x86 system."
            exit 1
        fi
        ;;
    x86_64)
        PLATFORM="x86"
        DETECTOR_IMG_DEFAULT="ghcr.io/sentania-labs/scarguard-detector-x86"
        ;;
    *)
        error "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac
info "Platform: ${PLATFORM} (${ARCH})"

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
    if [[ "$PLATFORM" == "jetson" ]]; then
        error "NVIDIA container runtime is not configured in Docker."
        NEEDS_SETUP=true
    else
        warn "NVIDIA container runtime not found — detector will use CPU inference."
        warn "GPU inference requires: NVIDIA driver + nvidia-container-toolkit"
    fi
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

    # Backfill keys added in v0.10 (DETECTOR_IMAGE, COMPOSE_FILE) for upgrades
    # from older .env files that don't have them yet.
    if ! grep -q '^DETECTOR_IMAGE=' .env; then
        echo "DETECTOR_IMAGE=${DETECTOR_IMG_DEFAULT}" >> .env
        info "Backfilled DETECTOR_IMAGE=${DETECTOR_IMG_DEFAULT}"
    fi
    if ! grep -q '^REDIS_PASSWORD=' .env; then
        REDIS_PASS=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
        echo "REDIS_PASSWORD=${REDIS_PASS}" >> .env
        info "Backfilled REDIS_PASSWORD (generated random credential)"
    fi
    # v1.14: detection-event HMAC signing. Missing or empty value triggers
    # a one-time generation so upgrades start signing without operator action.
    if ! grep -q '^DETECTION_HMAC_KEY=.\+' .env; then
        HMAC_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
        if grep -q '^DETECTION_HMAC_KEY=' .env; then
            sed -i "s|^DETECTION_HMAC_KEY=.*|DETECTION_HMAC_KEY=${HMAC_KEY}|" .env
        else
            echo "DETECTION_HMAC_KEY=${HMAC_KEY}" >> .env
        fi
        info "Backfilled DETECTION_HMAC_KEY (signs Redis detection events)"
    fi
    if ! grep -q '^TRAINING_CONTROLLER_TOKEN=.\{32\}' .env; then
        CONTROLLER_TOKEN=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
        if grep -q '^TRAINING_CONTROLLER_TOKEN=' .env; then
            sed -i "s|^TRAINING_CONTROLLER_TOKEN=.*|TRAINING_CONTROLLER_TOKEN=${CONTROLLER_TOKEN}|" .env
        else
            echo "TRAINING_CONTROLLER_TOKEN=${CONTROLLER_TOKEN}" >> .env
        fi
        info "Backfilled TRAINING_CONTROLLER_TOKEN (protects detector lifecycle API)"
    fi
    if ! grep -q '^COMPOSE_FILE=' .env; then
        if [[ "$NVIDIA_OK" == "true" ]]; then
            echo "COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml" >> .env
            info "Backfilled COMPOSE_FILE with GPU override"
        else
            echo "COMPOSE_FILE=docker-compose.yml" >> .env
            info "Backfilled COMPOSE_FILE (CPU only)"
        fi
    fi
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

    # Generate a random Redis password for inter-service auth
    REDIS_PASS=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    sed -i "s/^REDIS_PASSWORD=.*/REDIS_PASSWORD=${REDIS_PASS}/" .env

    # Generate an HMAC key for signing detection events (v1.14+).
    # Detector signs; deterrent + notifier verify. Without this key, the
    # internal Redis bus is unauthenticated.
    HMAC_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
    sed -i "s|^DETECTION_HMAC_KEY=.*|DETECTION_HMAC_KEY=${HMAC_KEY}|" .env

    # Dedicated training controller API credential; intentionally not shared
    # with web, detector, notifier, deterrent, or other Compose peers.
    CONTROLLER_TOKEN=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    sed -i "s|^TRAINING_CONTROLLER_TOKEN=.*|TRAINING_CONTROLLER_TOKEN=${CONTROLLER_TOKEN}|" .env

    if [[ "$HTTP_PORT_VALUE" != "80" ]]; then
        sed -i "s/^HTTP_PORT=.*/HTTP_PORT=${HTTP_PORT_VALUE}/" .env
    fi

    # Set detector image based on detected platform
    sed -i "s|^DETECTOR_IMAGE=.*|DETECTOR_IMAGE=${DETECTOR_IMG_DEFAULT}|" .env

    # Set compose files — include GPU override when NVIDIA runtime is available
    if [[ "$NVIDIA_OK" == "true" ]]; then
        sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml|" .env
    else
        sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml|" .env
    fi

    info "Created .env (HTTP_PORT=${HTTP_PORT_VALUE}, platform=${PLATFORM})"
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

# ── Step 7: TLS setup (first install only) ──────────────────────────────────
if [[ "$IS_UPGRADE" == "true" ]]; then
    step "TLS configuration"
    info "Existing install detected — TLS settings preserved. Change via Settings > TLS."
else
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
                # Update tls section in config volume (idempotent — works regardless of current value)
                docker run --rm -v scarguard-config:/config alpine:3.20 \
                    sh -c "sed -i 's/mode: \"[^\"]*\"/mode: \"auto\"/' /config/scarguard.yml && \
                           sed -i 's/domain: \"[^\"]*\"/domain: \"${TLS_DOMAIN}\"/' /config/scarguard.yml"
                info "TLS mode set to auto (Let's Encrypt) with domain: ${TLS_DOMAIN}"
                warn "Ports 80 and 443 must be reachable from the internet for ACME challenges."
            else
                warn "No domain provided — keeping TLS off. Change in Settings > TLS later."
            fi
            ;;
        3)
            info "TLS mode: manual. Place cert.pem and key.pem in the config volume's certs/ directory."
            docker run --rm -v scarguard-config:/config alpine:3.20 \
                sh -c "sed -i 's/mode: \"[^\"]*\"/mode: \"manual\"/' /config/scarguard.yml"
            info "Set TLS mode to manual in scarguard.yml."
            warn "HTTPS will activate once cert and key files are present at the configured paths."
            ;;
        *)
            info "TLS disabled (HTTP only). You can enable it later in Settings > TLS."
            ;;
    esac
fi

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
elif [[ "$IS_UPGRADE" == "true" ]]; then
    warn "No model file found. Upload via the web UI Models page."
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

# ── Step 10: Create initial admin account (first install only) ────────────────
if [[ "$IS_UPGRADE" == "true" ]]; then
    # Skip admin account creation on upgrade — account already exists
    :
else
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
fi

# ── Start / Restart ──────────────────────────────────────────────────────────

# Determine likely IP/URL for this host
HOST_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' || echo "YOUR_HOST_IP")
if [[ "$HTTP_PORT_FINAL" == "80" ]]; then
    WEB_URL="http://${HOST_IP}"
else
    WEB_URL="http://${HOST_IP}:${HTTP_PORT_FINAL}"
fi

if [[ "$IS_UPGRADE" == "true" ]]; then
    step "Restarting services"
    if docker compose up -d; then
        info "Services restarted with updated images."
    else
        error "Failed to restart services. Run manually: docker compose up -d"
    fi

    echo
    echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}${GREEN}║  Upgrade complete!                                           ║${RESET}"
    echo "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo
    echo "  Web UI: ${BOLD}${WEB_URL}${RESET}"
    echo
    echo "  To view logs:     docker compose logs -f"
    echo "  To stop:          docker compose down"
    echo
else
    step "Starting ScarGuard"
    if docker compose up -d; then
        info "Services started."
    else
        error "Failed to start services. Run manually: docker compose up -d"
    fi

    echo
    echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}${GREEN}║  Setup complete!                                             ║${RESET}"
    echo "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo

    if [[ "$NVIDIA_OK" == "false" ]]; then
        if [[ "$PLATFORM" == "jetson" ]]; then
            echo "${YELLOW}Reminder:${RESET} NVIDIA container runtime was not detected."
            echo "  The detector service (GPU inference) will fail to start."
            echo "  Log out and back in, then run:  docker compose up -d"
        else
            echo "${YELLOW}Note:${RESET} Running in CPU-only mode (no NVIDIA runtime detected)."
            echo "  Inference will be slower. Install nvidia-container-toolkit for GPU acceleration."
        fi
        echo
    fi

    echo "${BOLD}Next steps:${RESET}"
    echo
    echo "  1. ${BOLD}Open the web UI:${RESET}"
    echo "       ${WEB_URL}"
    echo
    echo "  2. ${BOLD}Configure your cameras:${RESET}"
    echo "       Go to Settings and add your RTSP camera URLs."
    if [[ -z "$MODEL_FILES" ]]; then
        echo
        echo "  3. ${BOLD}Add a YOLO model:${RESET}"
        echo "       Upload via the Models page in the admin menu."
    fi
    echo
    echo "  To view logs:     docker compose logs -f"
    echo "  To stop:          docker compose down"
    echo "  To update:        git pull && sudo bash setup.sh"
    echo
fi
