"""Single-process HTTP API, loaded only by the optional server entry point."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from .lens import LensRateLimitError, _search_bytes, status
from .server_images import MAX_IMAGE_BYTES, APIError, download_image, too_large, validate_image


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lang: str | None = Field(default=None, pattern=r"^[a-zA-Z]{1,8}(-[a-zA-Z0-9]{1,8})*$")
    max_matches: int | None = Field(default=None, ge=0)
    max_titles: int | None = Field(default=None, ge=0)
    resolve_links: bool = False


class URLSearch(SearchOptions):
    image_url: str = Field(min_length=1, max_length=8192)


class FileSearch(SearchOptions):
    image: bytes


class BodyTooLarge(MultiPartException):
    pass


async def _bounded_stream(request: Request, limit: int):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge("Request exceeds size limit.")
        yield chunk


async def _read_input(request: Request) -> tuple[bytes | str, SearchOptions]:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    try:
        if media_type == "application/json":
            body = bytearray()
            async for chunk in _bounded_stream(request, 64 * 1024):
                body.extend(chunk)
            payload = URLSearch.model_validate_json(bytes(body))
            return payload.image_url, SearchOptions(**payload.model_dump(exclude={"image_url"}))
        if media_type != "multipart/form-data":
            raise APIError(415, "unsupported_media_type", "Use JSON or multipart/form-data.")
        parser = MultiPartParser(request.headers, _bounded_stream(request, MAX_IMAGE_BYTES + 65536),
                                 max_files=1, max_fields=4, max_part_size=8192)
        form = await parser.parse()
        try:
            fields = dict(form)
            image = fields.pop("image", None)
            if len(form.multi_items()) != len(form) or not isinstance(image, UploadFile):
                raise ValueError("Expected a single image file.")
            if image.size is not None and image.size > MAX_IMAGE_BYTES:
                raise too_large()
            return await image.read(), SearchOptions.model_validate(fields)
        finally:
            await form.close()
    except BodyTooLarge as exc:
        raise APIError(413, "request_too_large", "Request exceeds the size limit.") from exc
    except (MultiPartException, MultipartParseError, ValidationError, ValueError) as exc:
        raise APIError(422, "invalid_request", "Invalid search input or options.") from exc


def _lookup(source: bytes | str, options: SearchOptions) -> dict:
    data = download_image(source) if isinstance(source, str) else source
    validate_image(data)
    try:
        result = _search_bytes(data, raise_on_rate_limit=True, **options.model_dump())
    except LensRateLimitError as exc:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise APIError(429, "upstream_rate_limited",
                       "Google Lens rate limited this search.", headers=headers) from exc
    if result is None:
        raise APIError(502, "lens_lookup_failed", "Lens did not return a usable results page.")
    return {"top_title": result.top_title, "titles": result.titles,
            "matches": [asdict(match) for match in result.matches]}


def create_app() -> FastAPI:
    app = FastAPI(title="glens", version="1")

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}},
                            status_code=exc.status, headers=exc.headers)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": {"code": "http_error", "message": str(exc.detail)}},
                            status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return JSONResponse({"error": {"code": "internal_error", "message": "Search failed."}},
                            status_code=500)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/v1/status")
    def session_status():
        info = status()
        return {k: info[k] for k in ("default_backend", "cache_enabled", "req_ready",
                                     "jar_age_seconds")}

    schemas = {"application/json": URLSearch, "multipart/form-data": FileSearch}
    request_body = {"required": True, "content": {
        media: {"schema": model.model_json_schema()} for media, model in schemas.items()
    }}

    @app.post("/v1/search", openapi_extra={"requestBody": request_body})
    async def search_endpoint(request: Request):
        source, options = await _read_input(request)
        worker = asyncio.get_running_loop().run_in_executor(None, _lookup, source, options)
        worker.add_done_callback(lambda future: future.exception()
                                 if not future.cancelled() else None)
        # Cancellation of an HTTP handler cannot stop a running browser thread.
        return await asyncio.shield(worker)

    return app


app = create_app()
