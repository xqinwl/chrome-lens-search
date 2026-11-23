"""Single-process HTTP API, loaded only by the optional server entry point."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from . import lens as _lens_mod
from .lens import (
    LensBotChallengeError,
    LensProxyError,
    LensRateLimitError,
    _search_bytes,
    status,
)
from .pool import RoutePool
from .server_images import (
    MAX_IMAGE_BYTES,
    APIError,
    compress_image_for_proxy,
    download_image,
    normalize_image_for_lens,
    too_large,
    validate_image,
)

_pool_lock = threading.Lock()
_route_pool: RoutePool | None = None


def _get_pool() -> RoutePool:
    global _route_pool
    with _pool_lock:
        if _route_pool is None:
            _route_pool = RoutePool.from_env()
        return _route_pool


def reset_pool(pool: RoutePool | None = None) -> None:
    """Replace or clear the process-wide route pool (handy for tests and reloads)."""
    global _route_pool
    with _pool_lock:
        _route_pool = pool


class _SearchPayload(dict):
    """JSON-serializable dict carrying the route label for X-Glens-Route."""

    def __init__(self, data: dict, route: str | None = None):
        super().__init__(data)
        self.route = route


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lang: str | None = Field(default=None, pattern=r"^[a-zA-Z]{1,8}(-[a-zA-Z0-9]{1,8})*$")
    max_matches: int | None = Field(default=None, ge=0)
    max_titles: int | None = Field(default=None, ge=0)
    resolve_links: bool = False
    proxy: Literal["vercel"] | None = None


class URLSearch(SearchOptions):
    image_url: str = Field(min_length=1, max_length=8192)


class FileSearch(SearchOptions):
    image: bytes


class WarmupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: str | None = Field(default=None, max_length=64)
    reset_cooldown: bool = True
    force: bool = True


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


async def _read_warmup_input(request: Request) -> WarmupRequest:
    body = bytearray()
    try:
        async for chunk in _bounded_stream(request, 16 * 1024):
            body.extend(chunk)
        if not body.strip():
            return WarmupRequest()
        return WarmupRequest.model_validate_json(bytes(body))
    except BodyTooLarge as exc:
        raise APIError(413, "request_too_large", "Request exceeds the size limit.") from exc
    except (ValidationError, ValueError) as exc:
        raise APIError(422, "invalid_request", "Invalid warmup options.") from exc


def _lookup(source: bytes | str, options: SearchOptions) -> dict:
    if options.proxy is not None and not isinstance(source, str):
        raise APIError(422, "proxy_requires_image_url",
                       "The Vercel proxy requires an HTTP(S) image_url input.")
    if options.proxy is not None and options.resolve_links:
        raise APIError(422, "proxy_option_unsupported",
                       "resolve_links is not supported with the Vercel proxy.")
    data = download_image(source) if isinstance(source, str) else source
    validate_image(data)
    data = normalize_image_for_lens(data)
    if options.proxy is not None:
        data = compress_image_for_proxy(data)
    route_id: str | None = None
    try:
        if _search_bytes is not _lens_mod._search_bytes:
            result = _search_bytes(data, raise_on_rate_limit=True, raise_on_proxy_error=True,
                                   **options.model_dump())
        else:
            result, route_id = _get_pool().search(data, **options.model_dump())
    except LensRateLimitError as exc:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise APIError(429, "upstream_rate_limited",
                       "Google Lens rate limited this search.", headers=headers) from exc
    except LensBotChallengeError as exc:
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        raise APIError(503, "upstream_bot_challenge",
                       "Google Lens presented a bot challenge; route is cooling down.",
                       headers=headers) from exc
    except LensProxyError as exc:
        raise APIError(502, exc.code, "The Vercel Lens proxy request failed.") from exc
    if result is None:
        raise APIError(502, "lens_lookup_failed", "Lens did not return a usable results page.")
    return _SearchPayload(
        {"top_title": result.top_title, "titles": result.titles,
         "matches": [asdict(match) for match in result.matches]},
        route=route_id,
    )


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

    @app.get("/v1/pool/status")
    def pool_status():
        return _get_pool().status()

    @app.post("/v1/warmup")
    async def warmup_endpoint(request: Request):
        opts = await _read_warmup_input(request)
        pool = _get_pool()
        worker = asyncio.get_running_loop().run_in_executor(
            None,
            lambda: pool.warmup(
                slot_id=opts.slot_id,
                reset_cooldown=opts.reset_cooldown,
                force=opts.force,
            ),
        )
        worker.add_done_callback(lambda future: future.exception()
                                 if not future.cancelled() else None)
        return await asyncio.shield(worker)

    schemas = {"application/json": URLSearch, "multipart/form-data": FileSearch}
    request_body = {"required": True, "content": {
        media: {"schema": model.model_json_schema()} for media, model in schemas.items()
    }}

    @app.post("/v1/search", openapi_extra={"requestBody": request_body})
    async def search_endpoint(request: Request, response: Response):
        source, options = await _read_input(request)
        worker = asyncio.get_running_loop().run_in_executor(None, _lookup, source, options)
        worker.add_done_callback(lambda future: future.exception()
                                 if not future.cancelled() else None)
        # Cancellation of an HTTP handler cannot stop a running browser thread.
        payload = await asyncio.shield(worker)
        route = getattr(payload, "route", None)
        if route:
            response.headers["X-Glens-Route"] = route
        return payload

    return app


app = create_app()
