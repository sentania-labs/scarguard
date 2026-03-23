"""ScarGuard web service — FastAPI application entry point."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from routes import about, config, dashboard, events, feed, models

app = FastAPI(title="ScarGuard")

# ── Static assets ──────────────────────────────────────────────────────────────
_src = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_src / "static")), name="static")

# Snapshots and model files live on shared volumes; serve them directly.
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")

Path(SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)

app.mount("/snapshots", StaticFiles(directory=SNAPSHOT_DIR), name="snapshots")
app.mount("/model-files", StaticFiles(directory=MODELS_DIR), name="model-files")

# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(dashboard.router)
app.include_router(events.router)
app.include_router(config.router)
app.include_router(models.router)
app.include_router(feed.router)
app.include_router(about.router)
