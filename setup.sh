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
    info ".env already exists — skipping."
else
    # Prompt for web port
    ask "Web UI port? (press Enter for default 8080): "
    read -r WEB_PORT_INPUT </dev/tty
    WEB_PORT_VALUE="${WEB_PORT_INPUT:-8080}"

    # Validate it's a number in a reasonable range
    if ! [[ "$WEB_PORT_VALUE" =~ ^[0-9]+$ ]] || \
       [[ "$WEB_PORT_VALUE" -lt 1 ]] || \
       [[ "$WEB_PORT_VALUE" -gt 65535 ]]; then
        warn "Invalid port '$WEB_PORT_VALUE' — using 8080."
        WEB_PORT_VALUE=8080
    fi

    cp .env.example .env
    # Update WEB_PORT in the new .env if user provided a non-default value
    if [[ "$WEB_PORT_VALUE" != "8080" ]]; then
        sed -i "s/^WEB_PORT=.*/WEB_PORT=${WEB_PORT_VALUE}/" .env
    fi

    info "Created .env (WEB_PORT=${WEB_PORT_VALUE})"
fi

# Read WEB_PORT for use in the final message
WEB_PORT_FINAL=$(grep -E '^WEB_PORT=' .env | cut -d= -f2 || echo "8080")

# ── Step 5: Create config/scarguard.yml ───────────────────────────────────────
step "Setting up configuration"

mkdir -p config

if [[ -f "config/scarguard.yml" ]]; then
    info "config/scarguard.yml already exists — skipping."
    CONFIG_IS_NEW=false
else
    cp config/scarguard.example.yml config/scarguard.yml
    info "Created config/scarguard.yml from example."
    CONFIG_IS_NEW=true
fi

# ── Step 6: Create data and models directories ────────────────────────────────
step "Creating directories"

mkdir -p data/snapshots
info "data/snapshots/ ready"

mkdir -p models
info "models/ ready"

# ── Step 7: Model check / starter model download ──────────────────────────────
step "Checking for YOLO model"

MODEL_FILES=$(find models/ -maxdepth 1 \( -name "*.pt" -o -name "*.engine" \) 2>/dev/null | head -5)

if [[ -n "$MODEL_FILES" ]]; then
    info "Model file(s) found in models/:"
    while IFS= read -r f; do
        info "  $(basename "$f")"
    done <<< "$MODEL_FILES"
else
    warn "No model file found in models/."
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
    echo "    Train your own YOLO model on heron/wildlife images and place the"
    echo "    .pt or .engine file in the models/ directory, then update"
    echo "    config/scarguard.yml → detection.model_path."
    echo "    See: https://docs.ultralytics.com/modes/train/"
    echo

    if confirm "Download the starter model (yolov8n.pt) now?" "y"; then
        STARTER_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
        STARTER_PATH="models/yolov8n.pt"

        if command -v curl &>/dev/null; then
            echo "  Downloading yolov8n.pt..."
            if curl -fL --progress-bar "$STARTER_URL" -o "$STARTER_PATH"; then
                info "Downloaded: models/yolov8n.pt"
                STARTER_DOWNLOADED=true
            else
                error "Download failed. Check your internet connection and try again."
                warn "You can download manually:"
                warn "  curl -L $STARTER_URL -o models/yolov8n.pt"
                STARTER_DOWNLOADED=false
            fi
        elif command -v wget &>/dev/null; then
            echo "  Downloading yolov8n.pt..."
            if wget -q --show-progress "$STARTER_URL" -O "$STARTER_PATH"; then
                info "Downloaded: models/yolov8n.pt"
                STARTER_DOWNLOADED=true
            else
                error "Download failed. Check your internet connection and try again."
                warn "You can download manually:"
                warn "  wget $STARTER_URL -O models/yolov8n.pt"
                STARTER_DOWNLOADED=false
            fi
        else
            error "Neither curl nor wget is available. Download the starter model manually:"
            warn "  curl -L $STARTER_URL -o models/yolov8n.pt"
            STARTER_DOWNLOADED=false
        fi

        # If the config was just created and the starter model downloaded,
        # update the model_path and target_classes to match yolov8n.
        if [[ "$STARTER_DOWNLOADED" == "true" && "$CONFIG_IS_NEW" == "true" ]]; then
            # The example config already uses yolov8n.pt and [bird] — no changes needed.
            info "config/scarguard.yml is pre-configured for the starter model."
        fi
    else
        warn "Skipping starter model download."
        warn "Add your model to models/ and update config/scarguard.yml before starting."
    fi
fi

# ── Step 8: Pull images ───────────────────────────────────────────────────────
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
    warn "This may be a network issue. Check your connection and try again."
    warn "  docker compose pull"
    # Don't exit — user can still review the config and pull later.
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo "${BOLD}${GREEN}║  Setup complete!                                             ║${RESET}"
echo "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo

echo "${BOLD}Next steps:${RESET}"
echo
echo "  1. ${BOLD}Edit your configuration:${RESET}"
echo "       nano config/scarguard.yml"
echo
echo "     Key settings to update:"
echo "       - cameras[*].rtsp_url   → Your UniFi Protect RTSP stream URLs"
echo "       - notifications.discord → Enable and add your webhook URL"
echo "       - detection.model_path  → Path to your model (already set if you"
echo "                                 downloaded the starter model)"
echo

if [[ -z "$MODEL_FILES" ]]; then
    echo "  2. ${BOLD}Add a YOLO model${RESET} (if you skipped the starter download):"
    echo "       Copy your .pt or .engine file to models/"
    echo "       Then update detection.model_path in config/scarguard.yml"
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
echo "       http://${ORIN_IP}:${WEB_PORT_FINAL}"
echo

if [[ "$NVIDIA_OK" == "false" ]]; then
    echo "${YELLOW}Reminder:${RESET} NVIDIA container runtime was not detected."
    echo "  The detector service (GPU inference) will fail to start."
    echo "  Log out and back in, then run:  docker compose up -d"
    echo
fi

if [[ "$CONFIG_IS_NEW" == "true" ]]; then
    echo "${YELLOW}Important:${RESET} config/scarguard.yml was created from the example."
    echo "  Update your RTSP camera URLs before starting — the system will not"
    echo "  detect anything until real camera streams are configured."
    echo
fi

echo "  To view logs:     docker compose logs -f"
echo "  To stop:          docker compose down"
echo "  To update images: docker compose pull && docker compose up -d"
echo
