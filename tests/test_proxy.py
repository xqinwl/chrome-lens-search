import pytest
import requests
from requests.cookies import RequestsCookieJar, extract_cookies_to_jar

from glens import lens


def test_vercel_adapter_wraps_lens_request_and_preserves_upstream_response():
    adapter = lens._VercelProxyAdapter("https://api.example/api/lens-proxy", "secret")
    seen = {}

    def forward(url, **kwargs):
        seen.update(url=url, **kwargs)
        response = requests.Response()
        response.status_code = 302
        response.url = url
        response.headers.update({
            "x-glens-proxy-upstream": "1",
            "location": "https://lens.google.com/search?udm=26&q=test",
            "set-cookie": "SID=updated; Path=/; Secure",
        })
        response._content = b"redirect body"
        response.raw = type("Raw", (), {
            "headers": type("Headers", (), {
                "getlist": lambda self, name: ["SID=updated; Path=/; Secure"]
            })()
        })()
        return response

    adapter.transport.post = forward
    prepared = requests.Request(
        "POST", "https://lens.google.com/v3/upload", data=b"multipart payload",
        headers={"User-Agent": "test-agent", "Cookie": "SID=old"},
    ).prepare()
    try:
        response = adapter.send(prepared, timeout=9)
    finally:
        adapter.close()

    assert seen["url"] == "https://api.example/api/lens-proxy"
    assert seen["data"] == b"multipart payload"
    assert seen["timeout"] == 9
    assert seen["allow_redirects"] is False
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["headers"]["X-Glens-Target-URL"] == prepared.url
    assert seen["headers"]["X-Glens-Target-Method"] == "POST"
    assert seen["headers"]["X-Glens-Forward-cookie"] == "SID=old"
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://lens.google.com/search")
    assert response.headers["set-cookie"].startswith("SID=updated")
    assert response.content == b"redirect body"
    jar = RequestsCookieJar()
    extract_cookies_to_jar(jar, prepared, response.raw)
    assert jar.get("SID") == "updated"


def test_vercel_adapter_forwards_arbitrary_http_targets():
    adapter = lens._VercelProxyAdapter("https://api.example/api/lens-proxy", "secret")
    seen = {}

    def forward(url, **kwargs):
        seen.update(url=url, **kwargs)
        response = requests.Response()
        response.status_code = 200
        response.headers["x-glens-proxy-upstream"] = "1"
        response._content = b"ok"
        return response

    adapter.transport.post = forward
    prepared = requests.Request("GET", "https://example.com/search").prepare()
    try:
        response = adapter.send(prepared)
    finally:
        adapter.close()
    assert seen["headers"]["X-Glens-Target-URL"] == "https://example.com/search"
    assert response.status_code == 200
    assert response.content == b"ok"


def test_vercel_adapter_rejects_non_http_targets():
    adapter = lens._VercelProxyAdapter("https://api.example/api/lens-proxy", "secret")
    adapter.transport.post = lambda *args, **kwargs: pytest.fail("proxy must not be called")
    prepared = requests.Request("GET", "ftp://example.com/file").prepare()
    try:
        with pytest.raises(lens.LensProxyError) as exc:
            adapter.send(prepared)
    finally:
        adapter.close()
    assert exc.value.code == "proxy_target_rejected"


def test_vercel_adapter_rejects_malformed_port():
    adapter = lens._VercelProxyAdapter("https://api.example/api/lens-proxy", "secret")
    adapter.transport.post = lambda *args, **kwargs: pytest.fail("proxy must not be called")
    prepared = requests.PreparedRequest()
    prepared.method = "GET"
    prepared.url = "https://example.com:bad/path"
    prepared.headers = {}
    try:
        with pytest.raises(lens.LensProxyError) as exc:
            adapter.send(prepared)
    finally:
        adapter.close()
    assert exc.value.code == "proxy_target_rejected"


def test_vercel_adapter_rejects_proxy_platform_429_as_not_upstream():
    adapter = lens._VercelProxyAdapter("https://api.example/api/lens-proxy", "secret")

    def proxy_rate_limit(*args, **kwargs):
        response = requests.Response()
        response.status_code = 429
        response._content = b"platform throttled"
        return response

    adapter.transport.post = proxy_rate_limit
    prepared = requests.Request("GET", "https://lens.google.com/search?udm=26").prepare()
    try:
        with pytest.raises(lens.LensProxyError) as exc:
            adapter.send(prepared)
    finally:
        adapter.close()
    assert exc.value.code == "proxy_unavailable"
    assert exc.value.upstream_status == 429


def test_proxy_search_without_cookie_jar_fails_without_browser_fallback(tmp_path):
    with pytest.raises(lens.LensProxyError) as exc:
        lens._search_via_requests(b"image", tmp_path / "missing.json", proxy="vercel")
    assert exc.value.code == "proxy_cookie_missing"


def test_proxy_uses_requests_when_process_default_is_browser(monkeypatch, tmp_path):
    seen = {}

    def search(*args, **kwargs):
        seen.update(kwargs)
        return [], "https://lens.google.com/search?udm=26"

    monkeypatch.setattr(lens, "DEFAULT_BACKEND", "browser")
    monkeypatch.setattr(lens, "_search_via_requests", search)
    result = lens._search_bytes(
        lens._tiny_png(), cookie_file=tmp_path / "cookies.json", proxy="vercel",
        use_cache=False,
    )
    assert seen["proxy"] == "vercel"
    assert result is not None and result.matches == []


def test_proxy_rejects_explicit_browser_backend():
    with pytest.raises(ValueError, match="requests backend"):
        lens._search_bytes(lens._tiny_png(), backend="browser", proxy="vercel")


def test_vercel_proxy_requires_https_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("GLENS_VERCEL_PROXY_URL", "http://api.example/api/lens-proxy")
    monkeypatch.setenv("GLENS_VERCEL_PROXY_SECRET", "secret")
    with pytest.raises(lens.LensProxyError) as exc:
        lens._req_session({"cookies": []}, proxy="vercel")
    assert exc.value.code == "proxy_config_invalid"
