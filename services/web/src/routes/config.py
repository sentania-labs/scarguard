from pathlib import Path

import config_store
import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/config")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def config_page(request: Request):
    cfg = config_store.load()
    raw = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
    return templates.TemplateResponse(
        "config.html",
        {"request": request, "raw_yaml": raw, "saved": False, "error": None},
    )


@router.post("", response_class=HTMLResponse)
async def save_config(request: Request, raw_yaml: str = Form(...)):
    error = None
    saved = False
    try:
        cfg = yaml.safe_load(raw_yaml)
        if not isinstance(cfg, dict):
            raise ValueError("Config must be a YAML mapping")
        config_store.save(cfg)
        saved = True
        # Re-dump so formatting is normalised in the editor
        raw_yaml = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "config.html",
        {"request": request, "raw_yaml": raw_yaml, "saved": saved, "error": error},
    )
