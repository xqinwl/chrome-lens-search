"""Single-process HTTP API, loaded only by the optional server entry point."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal
from urllib.parse import urlsplit

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
from .logging_setup import configure_logging
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

logger = logging.getLogger("glens")
_REQ_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

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
    """JSON-serializable dict carrying telemetry metadata for response headers & logs."""

    def __init__(
        self,
        data: dict,
        route: str | None = None,
        *,
        raw_bytes: int = 0,
        upload_bytes: int = 0,
    ):
        super().__init__(data)
        self.route = route
        self.raw_bytes = raw_bytes
        self.upload_bytes = upload_bytes


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "req_id", None)
    if existing:
        return existing
    incoming = (request.headers.get("x-request-id") or "").strip()
    req_id = incoming if _REQ_ID_RE.match(incoming) else uuid.uuid4().hex[:12]
    request.state.req_id = req_id
    return req_id


def _safe_source_label(source: bytes | str | None) -> str:
    if source is None:
        return "-"
    if isinstance(source, bytes):
        return f"file({len(source)}B)"
    try:
        parts = urlsplit(source)
        if parts.scheme and parts.hostname:
            return f"{parts.scheme}://{parts.hostname}"
    except ValueError:
        pass
    return "url(invalid)"


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
    raw_bytes = len(data)
    validate_image(data)
    data = normalize_image_for_lens(data)
    if options.proxy is not None:
        data = compress_image_for_proxy(data)
    upload_bytes = len(data)
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
        raw_bytes=raw_bytes,
        upload_bytes=upload_bytes,
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        configure_logging()
        pool = _get_pool()
        slot_summary = ",".join(
            f"{s.slot_id}({s.kind}:rpm={s.rpm_limit})" for s in pool.slots
        )
        logger.info(
            "glens server startup: slots=[%s] proxy_fallback=%s",
            slot_summary, pool.proxy_fallback,
        )
        yield
        logger.info("glens server shutdown.")

    app = FastAPI(title="glens", version="1", lifespan=_lifespan)

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError):
        req_id = _request_id(request)
        headers = dict(exc.headers)
        headers["X-Request-Id"] = req_id
        start_t = getattr(request.state, "start_time", None)
        duration_ms = int((time.monotonic() - start_t) * 1000) if start_t is not None else 0
        logger.warning(
            "http req_id=%s path=%s status=%d code=%s proxy=%s input=%s retry_after=%s "
            "duration_ms=%d msg=%s",
            req_id,
            request.url.path,
            exc.status,
            exc.code,
            getattr(request.state, "proxy", None) or "none",
            getattr(request.state, "source_label", "-"),
            headers.get("Retry-After", "-"),
            duration_ms,
            str(exc),
        )
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}},
                            status_code=exc.status, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        req_id = _request_id(request)
        headers = dict(exc.headers or {})
        headers["X-Request-Id"] = req_id
        logger.warning(
            "http req_id=%s path=%s status=%d code=http_error detail=%s",
            req_id, request.url.path, exc.status_code, exc.detail,
        )
        return JSONResponse({"error": {"code": "http_error", "message": str(exc.detail)}},
                            status_code=exc.status_code, headers=headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        req_id = _request_id(request)
        start_t = getattr(request.state, "start_time", None)
        duration_ms = int((time.monotonic() - start_t) * 1000) if start_t is not None else 0
        logger.exception(
            "http req_id=%s path=%s status=500 code=internal_error proxy=%s input=%s "
            "duration_ms=%d",
            req_id,
            request.url.path,
            getattr(request.state, "proxy", None) or "none",
            getattr(request.state, "source_label", "-"),
            duration_ms,
        )
        return JSONResponse({"error": {"code": "internal_error", "message": "Search failed."}},
                            status_code=500, headers={"X-Request-Id": req_id})

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
    async def warmup_endpoint(request: Request, response: Response):
        req_id = _request_id(request)
        t0 = time.monotonic()
        request.state.start_time = t0
        opts = await _read_warmup_input(request)
        pool = _get_pool()
        logger.info(
            "warmup req_id=%s start slot_id=%s force=%s reset_cooldown=%s",
            req_id, opts.slot_id or "all", opts.force, opts.reset_cooldown,
        )
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
        result = await asyncio.shield(worker)
        duration_ms = int((time.monotonic() - t0) * 1000)
        response.headers["X-Request-Id"] = req_id
        logger.info(
            "warmup req_id=%s status=200 duration_ms=%d slots=%s",
            req_id, duration_ms, result.get("slots"),
        )
        return result

    schemas = {"application/json": URLSearch, "multipart/form-data": FileSearch}
    request_body = {"required": True, "content": {
        media: {"schema": model.model_json_schema()} for media, model in schemas.items()
    }}

    @app.post("/v1/search", openapi_extra={"requestBody": request_body})
    async def search_endpoint(request: Request, response: Response):
        req_id = _request_id(request)
        t0 = time.monotonic()
        request.state.start_time = t0
        source, options = await _read_input(request)
        request.state.proxy = options.proxy
        request.state.source_label = _safe_source_label(source)
        worker = asyncio.get_running_loop().run_in_executor(None, _lookup, source, options)
        worker.add_done_callback(lambda future: future.exception()
                                 if not future.cancelled() else None)
        # Cancellation of an HTTP handler cannot stop a running browser thread.
        payload = await asyncio.shield(worker)
        duration_ms = int((time.monotonic() - t0) * 1000)
        route = getattr(payload, "route", None)
        response.headers["X-Request-Id"] = req_id
        if route:
            response.headers["X-Glens-Route"] = route
        logger.info(
            "search req_id=%s status=200 route=%s proxy=%s input=%s raw_bytes=%d "
            "upload_bytes=%d matches=%d titles=%d duration_ms=%d",
            req_id,
            route or "custom",
            options.proxy or "none",
            request.state.source_label,
            getattr(payload, "raw_bytes", 0),
            getattr(payload, "upload_bytes", 0),
            len(payload.get("matches", [])),
            len(payload.get("titles", [])),
            duration_ms,
        )
        return payload

    return app


app = create_app()
