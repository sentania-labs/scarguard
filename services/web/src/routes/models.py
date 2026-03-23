import os
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/models")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
ALLOWED_EXTENSIONS = {".pt", ".engine", ".onnx"}


@router.get("", response_class=HTMLResponse)
async def models_page(request: Request, uploaded: str = ""):
    files = sorted(
        [
            {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 1)}
            for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in ALLOWED_EXTENSIONS
        ],
        key=lambda x: str(x["name"]),
    )
    return templates.TemplateResponse(
        request,
        "models.html",
        {"files": files, "uploaded": uploaded, "error": None},
    )


@router.post("", response_class=HTMLResponse)
async def upload_model(request: Request, file: UploadFile = File(...)):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        files = _list_files()
        return templates.TemplateResponse(
            request,
            "models.html",
            {
                "files": files,
                "uploaded": "",
                "error": f"Unsupported file type '{suffix}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            },
        )

    dest = MODELS_DIR / Path(filename).name
    contents = await file.read()
    dest.write_bytes(contents)

    return RedirectResponse(url=f"/models?uploaded={file.filename}", status_code=303)


def _list_files() -> list[dict]:
    return sorted(
        [
            {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 1)}
            for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in ALLOWED_EXTENSIONS
        ],
        key=lambda x: str(x["name"]),
    )
