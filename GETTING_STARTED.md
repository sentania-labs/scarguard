# ScarGuard — Getting Started

## Setup Sequence

1. Set up Docker + NVIDIA runtime on the Orin
2. Build and start the GitHub Actions runner on the Orin
3. Create the ScarGuard repo on GitHub
4. Install Claude Code on your development machine
5. Start building the MVP with Claude Code

---

## Step 1: Set Up the Orin

SSH into your Orin and run the setup script:

```bash
# Clone the repo (or just copy the infra/ directory to start)
git clone https://github.com/YOUR_ORG/scarguard.git
cd scarguard

# Run the host setup script
sudo bash infra/orin-setup.sh

# IMPORTANT: Log out and back in for docker group to take effect
exit
# SSH back in

# Verify Docker + GPU
docker run --rm --runtime=nvidia --gpus all \
  dustynv/l4t-pytorch:r36.4.0 \
  python3 -c "import torch; print(torch.cuda.is_available())"
```

## Step 2: Start the GitHub Actions Runner

You need a GitHub Personal Access Token with `admin:org` scope (for org-level runner) or `repo` scope (for repo-level runner).

```bash
cd scarguard/infra

# Copy your internal CA cert into the runner build context
cp /path/to/sentania\ Lab\ Root\ 2.crt orin-runner/

# Create the .env file
cat > .env << 'EOF'
GITHUB_OWNER=your-github-org
GITHUB_TOKEN=ghp_your_token_here
# GITHUB_REPO=scarguard    # Uncomment for repo-level runner, omit for org-level
RUNNER_NAME=orin-nano
EOF

# Build and start the runner
docker compose -f docker-compose.runner.yml up -d --build

# Check it's running
docker compose -f docker-compose.runner.yml logs -f
```

You should see the runner register with GitHub. Verify in GitHub Settings → Actions → Runners — you should see `orin-nano` listed with labels `self-hosted, linux, arm64, jetson`.

### Updating the Runner

When you need to update the runner (new version, config changes):

```bash
cd scarguard/infra
git pull
docker compose -f docker-compose.runner.yml up -d --build
```

---

## Step 3: Create the GitHub Repo

If you haven't already:

```bash
cd scarguard
git init
git add -A
git commit -m "Initial project scaffold"

# Using GitHub CLI
gh repo create scarguard --private --source=. --push

# Or manually create on github.com and push:
# git remote add origin git@github.com:YOUR_ORG/scarguard.git
# git push -u origin main
```

---

## Step 4: Install Claude Code

### Requirements

- Claude Pro ($20/mo) or Max ($100-200/mo) subscription
- No Node.js required with the native installer

### Install (Linux/Mac/WSL)

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Open a new terminal, then authenticate:

```bash
claude
```

This opens your browser for a one-time sign-in.

### Install (Windows PowerShell)

```powershell
irm https://claude.ai/install.sh | iex
```

---

## Step 5: Build the MVP with Claude Code

### Start a session

```bash
cd scarguard
claude
```

Claude Code automatically reads `CLAUDE.md` from the repo root, so it knows the full architecture.

### Recommended build order

Build one phase at a time. Test each before moving on. Commit after each working milestone.

#### Phase 1: Detection Engine

```
Build the detector service. Start with:
1. Dockerfile based on dustynv/l4t-pytorch:r36.4.0
2. src/stream.py - RTSP stream reader using OpenCV with reconnect logic
3. src/detector.py - YOLO model wrapper that loads .engine or .pt files
4. src/events.py - Detection event processing with cooldown dedup
5. src/publisher.py - Redis pub/sub publisher
6. src/main.py - Main loop tying it all together
7. requirements.txt

Read CLAUDE.md for the full architecture and config schema.
Start with a single camera stream.
```

#### Phase 2: Notifications

```
Build the notifier service. It should:
1. Subscribe to Redis channel scarguard:detections
2. Dispatch to Discord webhook with snapshot image attached
3. Dispatch to email via SMTP with snapshot attachment
4. Read notification config from scarguard.yml
5. Include Dockerfile and requirements.txt

Reference CLAUDE.md for the config schema and project structure.
```

#### Phase 3: Web UI

```
Build the web service using FastAPI + Jinja2. Include:
1. Dashboard page with arm/disarm toggle and system status
2. Detection event log page with snapshot thumbnails
3. Config editor that reads/writes scarguard.yml
4. Model upload page (save to /models volume)
5. Live camera feed page with bounding box overlay via SSE
6. Dockerfile and requirements.txt

Use HTMX for dynamic updates where it makes sense.
Keep the UI clean and functional — not fancy.
Reference CLAUDE.md for architecture details.
```

#### Docker Compose

```
Create docker-compose.yml with:
- detector service (runtime: nvidia, GPU access)
- web service (port 8080)
- notifier service
- redis service
- Shared volumes for config/, models/, data/
Reference CLAUDE.md for the full service layout.
```

#### CI Workflows

```
Create GitHub Actions workflows:
1. .github/workflows/ci.yml — runs on x86 runners:
   - Lint (ruff or flake8)
   - Type check (mypy)
   - pytest for web and notifier services
   - Build and push web + notifier images to ghcr.io

2. .github/workflows/deploy.yml — runs on Orin runner (self-hosted, arm64, jetson):
   - Stop detector service
   - Build detector image (ARM64 + L4T base)
   - Run GPU smoke test
   - Push detector image to ghcr.io
   - docker compose pull + up

Reference CLAUDE.md for the CI/CD strategy and runner labels.
```

### Tips for working with Claude Code

- **Review diffs before accepting.** This is how you stay in control and learn what it's doing.
- **Ask it to explain.** "Explain what detector.py does" before moving on.
- **Iterate in small steps.** One service at a time, test, commit, next.
- **Use `/init`** on first run to let it scan the project structure.
- **Git commit often.** Commit after each working milestone so you can roll back.

### Useful Claude Code commands

```
/help           Show available commands
/init           Initialize project context
/clear          Clear conversation history
/cost           Show token usage for the session
```

---

## Development Workflow

**Recommended: Develop on your desktop, deploy to Orin.**

1. Run Claude Code on your main machine in the ScarGuard repo
2. Build and test non-GPU services locally (web, notifier)
3. Push to GitHub
4. CI runs on x86 runners (lint, test, build web/notifier images)
5. Deploy workflow runs on Orin runner (build detector, smoke test, deploy)

For quick iteration on the detector:
- SSH into the Orin
- `cd scarguard && git pull`
- `docker compose up --build detector`

---

## Reference: Runner Labels for Workflows

Use these in your workflow files:

```yaml
# For x86 jobs (lint, test, build non-GPU images)
runs-on: self-hosted  # Or your org's x86 runner labels

# For Orin jobs (detector build, GPU tests, deploy)
runs-on: [self-hosted, arm64, jetson]
```