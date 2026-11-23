import asyncio
import threading

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from glens import lens, server  # noqa: E402
from glens.cli import main  # noqa: E402
from glens.server_images import MAX_IMAGE_BYTES  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()),
                                 base_url="http://test") as test_client:
        yield test_client


async def test_url_and_upload_share_contract(client, monkeypatch):
    image = lens._tiny_png()
    monkeypatch.setattr(server, "download_image", lambda url: image)
    calls = []

    def search(frame, **opts):
        calls.append((frame, opts))
        return lens.LensResult("Shoe", ["Shoe"], [lens.Match("Shoe", "https://a.com", "a.com")],
                               results_url="private")

    monkeypatch.setattr(server, "_search_bytes", search)
    options = dict(lang="zh-CN", max_matches=3, max_titles=2, resolve_links=True)
    by_url = await client.post("/v1/search", json={"image_url": "https://cdn.example/a", **options})
    by_file = await client.post("/v1/search", data=options, files={"image": ("shoe.png", image)})
    assert by_url.status_code == by_file.status_code == 200
    assert by_file.json() == by_url.json() == {
        "top_title": "Shoe", "titles": ["Shoe"],
        "matches": [{"title": "Shoe", "url": "https://a.com", "domain": "a.com"}]}
    assert calls == [(image, {**options, "proxy": None, "raise_on_rate_limit": True,
                              "raise_on_proxy_error": True})] * 2


@pytest.mark.parametrize("outcome,code", [(None, 502), (lens.LensResult(None, [], []), 200)])
async def test_empty_and_inconclusive(client, monkeypatch, outcome, code):
    monkeypatch.setattr(server, "_search_bytes", lambda *a, **kw: outcome)
    r = await client.post("/v1/search", files={"image": ("a.png", lens._tiny_png())})
    assert r.status_code == code
    if code == 200:
        assert r.json() == {"top_title": None, "titles": [], "matches": []}
    else:
        assert r.json()["error"]["code"] == "lens_lookup_failed"


async def test_upstream_rate_limit_is_visible_to_client(client, monkeypatch):
    def rate_limited(*args, **kwargs):
        raise lens.LensRateLimitError("12")

    monkeypatch.setattr(server, "_search_bytes", rate_limited)
    r = await client.post("/v1/search", files={"image": ("a.png", lens._tiny_png())})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_rate_limited"
    assert r.headers["retry-after"] == "12"


async def test_vercel_proxy_requires_url_input_and_skips_link_resolution(client, monkeypatch):
    called = False

    def search(*args, **kwargs):
        nonlocal called
        called = True
        return lens.LensResult(None, [], [])

    monkeypatch.setattr(server, "_search_bytes", search)
    file_response = await client.post("/v1/search", data={"proxy": "vercel"},
                                      files={"image": ("a.png", lens._tiny_png())})
    assert file_response.status_code == 422
    assert file_response.json()["error"]["code"] == "proxy_requires_image_url"
    assert not called

    monkeypatch.setattr(server, "download_image", lambda _url: lens._tiny_png())
    links_response = await client.post("/v1/search", json={
        "image_url": "https://img.example/a", "proxy": "vercel", "resolve_links": True,
    })
    assert links_response.status_code == 422
    assert links_response.json()["error"]["code"] == "proxy_option_unsupported"
    assert not called


async def test_proxy_failure_is_returned_without_browser_fallback(client, monkeypatch):
    def proxy_error(*args, **kwargs):
        raise lens.LensProxyError("proxy_not_configured", "missing configuration")

    monkeypatch.setattr(server, "download_image", lambda _url: lens._tiny_png())
    monkeypatch.setattr(server, "_search_bytes", proxy_error)
    response = await client.post("/v1/search", json={
        "image_url": "https://img.example/a", "proxy": "vercel",
    })
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "proxy_not_configured"


async def test_requests_backend_searches_can_overlap(monkeypatch):
    started = threading.Barrier(3)

    def lookup(*_):
        started.wait(timeout=5)
        return {"top_title": None, "titles": [], "matches": []}

    monkeypatch.setattr(server, "_lookup", lookup)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.create_app()),
        base_url="http://test",
    ) as parallel_client:
        responses = await asyncio.gather(*[
            parallel_client.post("/v1/search", json={"image_url": "https://img.example/a"})
            for _ in range(3)
        ])
    assert [response.status_code for response in responses] == [200, 200, 200]


@pytest.mark.parametrize("kwargs,code", [
    ({"json": {"image_url": "/etc/passwd"}}, 422),
    ({"json": {"image_url": "file:///etc/passwd"}}, 422),
    ({"json": {"image_url": "https://a.example", "backend": "browser"}}, 422),
    ({"json": {"image_url": "https://a.example", "proxy": "arbitrary"}}, 422),
    ({"json": {"image_url": "http://a.example", "lang": "en\r\nX-Test"}}, 422),
    ({"data": {"image": "/tmp/photo"}}, 415),
    ({"files": {"image": ("a.png", b"not an image")}}, 415),
    ({"files": {"image": ("a.png", b"x" * (MAX_IMAGE_BYTES + 1))}}, 413),
    ({"content": b"{", "headers": {"Content-Type": "application/json"}}, 422),
])
async def test_bad_input(client, kwargs, code):
    r = await client.post("/v1/search", **kwargs)
    assert r.status_code == code
    assert set(r.json()["error"]) == {"code", "message"}


def test_cancellation_does_not_block_other_searches_or_health(monkeypatch):
    started = threading.Event()
    both_started = threading.Event()
    finish = threading.Event()
    count = 0
    count_lock = threading.Lock()

    def lookup(*_):
        nonlocal count
        with count_lock:
            count += 1
            started.set()
            if count >= 2:
                both_started.set()
        assert finish.wait(10)
        return {"titles": []}

    monkeypatch.setattr(server, "_lookup", lookup)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()),
                                     base_url="http://test") as client:
            def submit():
                return asyncio.create_task(client.post("/v1/search", json={"image_url": "test"}))

            running = submit()
            try:
                assert await asyncio.to_thread(started.wait, 3)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
                parallel = submit()
                assert await asyncio.to_thread(both_started.wait, 3)
                assert (await client.get("/healthz")).json() == {"ok": True}
                assert set((await client.get("/v1/status")).json()) == {
                    "req_ready", "default_backend", "cache_enabled", "jar_age_seconds"}
            finally:
                finish.set()
            assert (await parallel).status_code == 200
            assert (await submit()).status_code == 200

    asyncio.run(exercise())


def test_serve_command(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    assert main(["serve"]) == main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8000}, {"host": "0.0.0.0", "port": 9000}]
