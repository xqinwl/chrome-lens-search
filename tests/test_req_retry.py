"""Retry/backoff behaviour of the req backend, with a fake session, no network.

The synthetic HTML below is hand-written to mimic only the *shape* of a results
page (three external anchors); it contains no Google content.
"""

import json

import pytest
import requests

import glens.lens as lens_mod

SYNTHETIC_RESULTS_HTML = """
<html><body>
  <a href="https://a.example/p/1" aria-label="Acme Widget Pro Runner">t</a>
  <a href="https://b.example/p/2" aria-label="Acme Widget Pro Runner">t</a>
  <a href="https://c.example/p/3" aria-label="Different Gadget Deluxe">t</a>
</body></html>
"""
RESULTS_URL = "https://www.google.com/search?udm=26&synthetic=1"
JS_GATE_HTML = '<a href="/httpservice/retry/enablejs">enable</a>'


class FakeResponse:
    def __init__(self, url: str, text: str, status: int = 200):
        self.url = url
        self.text = text
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            response = requests.Response()
            response.status_code = self.status
            response.url = self.url
            if self.status == 429:
                response.headers["Retry-After"] = "8"
            raise requests.HTTPError(f"synthetic {self.status}", response=response)


class FlakySession:
    """Raises a transient transport error on the first N post() calls."""

    def __init__(self, fail_first: int, html: str = SYNTHETIC_RESULTS_HTML,
                 url: str = RESULTS_URL, status: int = 200):
        self.fail_first = fail_first
        self.posts = 0
        self.html = html
        self.url = url
        self.status = status

    def post(self, *args, **kwargs):
        self.posts += 1
        if self.posts <= self.fail_first:
            raise requests.ConnectionError("synthetic transport failure")
        return FakeResponse(self.url, self.html, self.status)

    def get(self, *args, **kwargs):
        return FakeResponse(self.url, self.html, self.status)

    def close(self):
        pass


@pytest.fixture
def jar_file(tmp_path):
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({
        "user_agent": "synthetic-agent/1.0",
        "cookies": [{"name": "TEST", "value": "1", "domain": ".google.com", "path": "/"}],
    }), encoding="utf-8")
    return jar


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lens_mod.time, "sleep", lambda seconds: None)


def test_retries_once_on_transient_failure(monkeypatch, jar_file):
    session = FlakySession(fail_first=1)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 2
    assert url == RESULTS_URL
    assert items is not None and len(items) == 3


def test_gives_up_after_all_attempts(monkeypatch, jar_file):
    session = FlakySession(fail_first=99)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == lens_mod._REQ_ATTEMPTS
    assert items is None and url is None


def test_js_gate_shell_does_not_retry(monkeypatch, jar_file):
    # A JS-gate shell means stale cookies, not a transient blip: fall back
    # immediately (one attempt) so the browser can re-mint the jar.
    session = FlakySession(fail_first=0, html=JS_GATE_HTML)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1
    assert items is None
    assert url == RESULTS_URL


def test_no_jar_short_circuits(tmp_path):
    items, url = lens_mod._search_via_requests(b"img", tmp_path / "missing-jar.json")
    assert items is None and url is None


def test_http_error_does_not_retry(monkeypatch, jar_file):
    # Upstream 429 is surfaced clearly and never retried through another backend.
    session = FlakySession(fail_first=0, status=429)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    with pytest.raises(lens_mod.LensRateLimitError) as exc:
        lens_mod._search_via_requests(b"img", jar_file)
    assert session.posts == 1
    assert exc.value.retry_after == "8"


def test_non_results_redirect_falls_back(monkeypatch, jar_file):
    # Upload redirected somewhere that is neither udm=26, lens, nor a bot challenge.
    session = FlakySession(fail_first=0, url="https://www.google.com/webhp")
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1
    assert items is None and url is None


def test_sorry_redirect_raises_bot_challenge_without_retry(monkeypatch, jar_file):
    session = FlakySession(fail_first=0, url="https://www.google.com/sorry/index")
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    with pytest.raises(lens_mod.LensBotChallengeError) as exc:
        lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1
    assert exc.value.challenge_url == "https://www.google.com/sorry/index"


def test_captcha_html_raises_bot_challenge(monkeypatch, jar_file):
    captcha_html = (
        '<html><body><form id="captcha-form">Our systems have detected unusual traffic '
        'from your computer network.</form></body></html>'
    )
    session = FlakySession(fail_first=0, url=RESULTS_URL, html=captcha_html)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    with pytest.raises(lens_mod.LensBotChallengeError):
        lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1


def test_req_rolls_updated_cookies_to_disk(monkeypatch, jar_file):
    session = FlakySession(fail_first=0, html=SYNTHETIC_RESULTS_HTML)
    session.cookies = requests.cookies.RequestsCookieJar()
    session.cookies.set("TEST", "1", domain=".google.com", path="/")
    session.cookies.set("NID", "rotated-token", domain=".google.com", path="/")
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert items is not None and url == RESULTS_URL
    saved = json.loads(jar_file.read_text(encoding="utf-8"))
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["TEST"] == "1"
    assert by_name["NID"] == "rotated-token"


def test_curl_cffi_session_shim_handles_multipart_and_cookies(monkeypatch):
    seen = {}

    class FakeInnerCookies:
        def __init__(self):
            self.items = []

        def set(self, name, value, domain=None, path=None):
            self.items.append(type("C", (), {
                "name": name, "value": value, "domain": domain, "path": path
            })())

        def __iter__(self):
            return iter(self.items)

    class FakeInnerSession:
        def __init__(self, impersonate=None):
            seen["impersonate"] = impersonate
            self.cookies = FakeInnerCookies()
            self.closed = False

        def post(self, url, **kwargs):
            seen["post"] = (url, kwargs)
            self.cookies.set("NID", "from-cffi", domain=".google.com", path="/")
            return type("R", (), {
                "status_code": 200, "url": RESULTS_URL,
                "text": SYNTHETIC_RESULTS_HTML, "headers": {},
            })()

        def close(self):
            self.closed = True

    fake_cffi = type("FakeCffi", (), {"Session": FakeInnerSession})
    monkeypatch.setattr(lens_mod, "_load_curl_cffi", lambda: fake_cffi)

    jar = {"user_agent": "Chrome/149.0.0.0", "cookies": []}
    session = lens_mod._req_session(jar, local_proxy="http://127.0.0.1:7897")
    resp = session.post(
        lens_mod._UPLOAD_ENDPOINT,
        files={"encoded_image": ("frame.jpg", b"jpeg-bytes", "image/jpeg")},
        data={"sbisrc": "Google Chrome"},
    )
    resp.raise_for_status()
    session.close()

    assert seen["impersonate"] == "chrome"
    url, kwargs = seen["post"]
    assert url == lens_mod._UPLOAD_ENDPOINT
    assert "multipart/form-data; boundary=" in kwargs["headers"]["Content-Type"]
    assert b"jpeg-bytes" in kwargs["data"]
    assert kwargs["proxies"] == {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
    assert session.cookies.get("NID") == "from-cffi"


def test_degraded_page_with_few_external_anchors_falls_back(monkeypatch, jar_file):
    degraded = '<a href="https://only.example/one" aria-label="Lonely Product Tile">t</a>'
    session = FlakySession(fail_first=0, html=degraded)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1
    assert items is None
    assert url == RESULTS_URL


def test_gate_marker_on_healthy_page_is_not_a_gate(monkeypatch, jar_file):
    # Healthy server-rendered pages also carry the enable-js noscript link;
    # the page must be judged by its anchors, not the marker (live-verified).
    healthy_with_marker = JS_GATE_HTML + SYNTHETIC_RESULTS_HTML
    session = FlakySession(fail_first=0, html=healthy_with_marker)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert items is not None and len(items) == 4
    assert url == RESULTS_URL


def test_chrome_header_surface():
    jar = {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/149.0.0.0 Safari/537.36",
           "cookies": []}
    headers = lens_mod._req_session(jar).headers

    # Google gates bare header sets even with a fresh jar (live-verified);
    # the full Chrome surface must be present and version-consistent.
    assert headers["Referer"] == "https://www.google.com/"
    assert headers["Sec-Fetch-Mode"] == "navigate"
    assert 'v="149"' in headers["sec-ch-ua"]
    assert headers["sec-ch-ua-platform"] == '"Windows"'


def test_chrome_headers_skip_ua_hints_for_non_chrome_ua():
    jar = {"user_agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/128.0",
           "cookies": []}
    headers = lens_mod._req_session(jar).headers
    assert "sec-ch-ua" not in headers
    assert headers["Referer"] == "https://www.google.com/"


def test_upload_that_never_redirects_falls_back(monkeypatch, jar_file):
    """resp.url still on the upload endpoint means the POST never redirected.

    The endpoint lives on lens.google.com, so the old "lens" substring check
    waved it through and the upload response body got parsed as a results page.
    """
    session = FlakySession(fail_first=0, url=lens_mod._UPLOAD_ENDPOINT,
                           html=SYNTHETIC_RESULTS_HTML)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert session.posts == 1
    assert items is None and url is None


def test_consent_interstitial_falls_back(monkeypatch, jar_file):
    # Carries "lens" in its continue= param, so it used to read as a results page.
    consent = "https://consent.google.com/m?continue=https://lens.google.com/search"
    session = FlakySession(fail_first=0, url=consent, html=SYNTHETIC_RESULTS_HTML)
    monkeypatch.setattr(lens_mod, "_req_session", lambda jar, lang=None: session)

    items, url = lens_mod._search_via_requests(b"img", jar_file)

    assert items is None and url is None
