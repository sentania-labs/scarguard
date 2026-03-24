import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/models")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
ALLOWED_EXTENSIONS = {".pt", ".engine", ".onnx"}
_DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_CHUNK_SIZE = int(os.environ.get("MODEL_UPLOAD_CHUNK_SIZE", str(_DEFAULT_CHUNK_SIZE)))
if UPLOAD_CHUNK_SIZE <= 0:
    raise ValueError(
        f"MODEL_UPLOAD_CHUNK_SIZE must be a positive integer (got {UPLOAD_CHUNK_SIZE}); "
        f"default is {_DEFAULT_CHUNK_SIZE} bytes"
    )
MAX_UPLOAD_BYTES = int(os.environ["MODEL_UPLOAD_MAX_BYTES"]) if "MODEL_UPLOAD_MAX_BYTES" in os.environ else None


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
    temp_file_path: Path | None = None
    bytes_written = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=MODELS_DIR,
            prefix=f".{dest.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file_path = Path(temp_file.name)
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if MAX_UPLOAD_BYTES is not None and bytes_written > MAX_UPLOAD_BYTES:
                    temp_file.close()
                    temp_file_path.unlink(missing_ok=True)
                    files = _list_files()
                    max_size_mb = round(MAX_UPLOAD_BYTES / 1_048_576, 1)
                    return templates.TemplateResponse(
                        request,
                        "models.html",
                        {
                            "files": files,
                            "uploaded": "",
                            "error": f"Upload exceeds max size of {max_size_mb} MB.",
                        },
                    )
                temp_file.write(chunk)

        os.replace(temp_file_path, dest)
    finally:
        await file.close()
        if temp_file_path and temp_file_path.exists() and temp_file_path != dest:
            temp_file_path.unlink(missing_ok=True)

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
