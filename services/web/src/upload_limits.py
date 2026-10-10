"""Bound request streams before CSRF/form parsing, including chunked bodies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import config_store
from config_model import UploadLimitsConfig
from fastapi import HTTPException, Request
from python_multipart.exceptions import MultipartParseError
from route_auth import require_admin
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MIB = 1024 * 1024
UPLOAD_PATHS = {"/models": "model_mb", "/admin/training/uploads": "dataset_mb"}


def upload_limits() -> UploadLimitsConfig:
    return UploadLimitsConfig.model_validate(
        (config_store.load_cached().get("system") or {}).get("uploads") or {}
    )


def file_limit(path: str) -> int:
    return int(getattr(upload_limits(), UPLOAD_PATHS[path])) * MIB


class FileLimitExceeded(MultiPartException):
    pass


class LimitedUploadParser(MultiPartParser):
    """Stop oversized file parts during parsing, closing partial spools."""

    def __init__(self, request: Request, limit: int) -> None:
        super().__init__(
            request.headers, request.stream(), max_files=1, max_fields=16, max_part_size=64 * 1024
        )
        self.limit = limit
        self.file_bytes = 0
        self.header_bytes = 0
        self.complete = False

    def on_end(self) -> None:
        self.complete = True
        super().on_end()

    def on_part_begin(self) -> None:
        self.header_bytes = 0
        super().on_part_begin()

    def _count_header(self, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > 64 * 1024:
            raise MultiPartException("Multipart headers too large")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(start, end)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(start, end)
        super().on_header_value(data, start, end)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > self.limit:
                raise FileLimitExceeded("File exceeds configured upload limit")
        super().on_part_data(data, start, end)


@asynccontextmanager
async def upload_form(request: Request, path: str) -> AsyncIterator[FormData]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise HTTPException(status_code=422, detail="Multipart file required")
    parser = LimitedUploadParser(request, file_limit(path))
    try:
        try:
            form = await parser.parse()
        except MultipartParseError as exc:
            raise HTTPException(status_code=400, detail="Malformed multipart upload") from exc
        except MultiPartException as exc:
            raise HTTPException(
                status_code=413 if isinstance(exc, FileLimitExceeded) else 400, detail=exc.message
            ) from exc
        if not parser.complete:
            raise HTTPException(status_code=400, detail="Incomplete multipart upload")
        yield form
    finally:
        # Include unfinished parts absent from FormData, and close on disconnect
        # or cancellation as well. Starlette also closes these on parser errors.
        for file in parser._files_to_close_on_error:
            file.close()


class RequestLimitMiddleware:
    """Count actual bytes; never drain a denied request or trust its length.

    MultiPartException makes Starlette close partial spooled files. Suppress
    the parser's 400 response and return 413 when our counter fired instead.
    Ordinary form/JSON bodies are capped at 1 MiB; upload envelopes get an
    additional 1 MiB beyond the separately enforced file limit.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path.rstrip("/")
        is_upload = request.method == "POST" and path in UPLOAD_PATHS
        if is_upload or (request.method == "POST" and path == "/config/tls/upload-cert"):
            gate = require_admin(request)
            if not isinstance(gate, dict):
                await gate(scope, receive, send)
                return
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        multipart = media_type == "multipart/form-data"
        limit = file_limit(path) + MIB if is_upload and multipart else MIB
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                await JSONResponse({"error": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
            if len(length.lstrip("0")) > 20 or int(length.lstrip("0") or "0") > limit:
                await JSONResponse({"error": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
                return
        consumed = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal consumed, exceeded
            if exceeded:
                raise MultiPartException("Request too large")
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    exceeded = True
                    raise MultiPartException("Request too large")
            return message

        async def limited_send(message: Message) -> None:
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await JSONResponse({"error": "Request too large"}, status_code=413)(
                scope, receive, send
            )
