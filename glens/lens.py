"""Google Lens visual product search over plain HTTP.

Lens is very good at naming the exact product in a photo, but the free Python
Lens libraries only do OCR/translation, and hitting Lens from a plain server just
gets you blocked. This sends the image straight to Google's own Lens upload
endpoint and reads the product matches back.

Every lookup is two steps: POST the image to lens.google.com/v3/upload (the
``encoded_image`` field), which redirects to a results URL carrying ``&udm=26``;
then read that URL and scrape the result anchors. The image only ever touches
Google, never a third-party host.

Two backends (``backend=`` / ``GLENS_BACKEND``, default "auto"):

  req      replays a cookie jar minted earlier by a real browser, over plain
           ``requests``. Google only server-renders the product anchors for a
           JS-verified session, so a cold client gets an "enable JS" shell; with
           the browser's cookies, though, the raw HTML has everything we need. If
           the jar is missing or stale, the browser path runs and re-mints it.
  browser  drives undetected-chromedriver, uploads by submitting an in-page
           multipart form (a navigation, so the cross-origin POST isn't blocked
           by CORS), renders the page and scrapes the anchors. Every good run
           saves the google.com cookies + UA back to the jar so req can reuse them.
  auto     req first, browser as a fallback. This is the default.

Result tiles link out through Google's own redirect wrapper
(``google.com/goto?url=<blob>``). The blob is encrypted, so the merchant URL is
nowhere in the page; we resolve each wrapper with one cheap request that reads
the ``Location`` header and stops, never touching the merchant's own site.

The parser stays selector-light on purpose. Lens markup uses randomized, rotating
class names, so instead of hardcoding classes we just grab every anchor and mine
its href and label. That's high-recall and a little noisy; filter it downstream
if you need precision.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import re
import struct
import threading
import time
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from . import cache as _cache_mod

logger = logging.getLogger("glens")


def _env_flag(name: str) -> bool:
    """Truthy env flag ("1", "true", "yes", "on"); anything else is off."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    """Read a lowercased string env default. Blank means "not configured": a
    container's ``-e GLENS_FOO=`` (or an empty line in a .env) must land on the
    default, never on an empty setting."""
    return os.getenv(name, "").strip().lower() or default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read an int env default; malformed values must never break `import glens`."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("glens: ignoring non-integer %s=%r (using %d).", name, raw, default)
        return default


DEFAULT_BACKEND = _env_str("GLENS_BACKEND", "auto")
DEFAULT_MAX_MATCHES = _env_int("GLENS_MAX_MATCHES", 20)
DEFAULT_MAX_TITLES = _env_int("GLENS_MAX_TITLES", 6)
DEFAULT_RESULT_TIMEOUT = _env_int("GLENS_RESULT_TIMEOUT", 25, minimum=5)
DEFAULT_LANG = os.getenv("GLENS_LANG", "").strip() or None
DEFAULT_USE_CACHE = _env_flag("GLENS_CACHE")
DEFAULT_CACHE_TTL = _env_int("GLENS_CACHE_TTL", 86400)
DEFAULT_RESOLVE_MAX = _env_int("GLENS_RESOLVE_MAX", 5, minimum=1)

_VALID_BACKENDS = ("auto", "req", "browser")
_REQ_ATTEMPTS = 2
_REQ_BACKOFF_SECONDS = 1.5
# Headroom on top of the caller's timeout for the upload navigation itself
# (POST the image, follow Google's redirect) before the results page has to load.
_UPLOAD_GRACE_SECONDS = 30


def _default_cookie_file() -> Path:
    env = os.getenv("GLENS_COOKIE_FILE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".cache" / "glens" / "cookies.json"


def _default_cache_dir() -> Path:
    return _cache_mod.default_cache_dir()


# Lens' own upload endpoint. The old reverse-image-search one
# (www.google.com/searchbyimage/upload) now answers 404 for everything, which
# took every lookup down with it; this one still redirects to the &udm=26
# results URL both backends expect (re-verified live 2026-07).
_UPLOAD_ENDPOINT = "https://lens.google.com/v3/upload"
_LANDING_PAGE = "https://www.google.com/"
_REQ_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
# Google serves this shell instead of results when the session isn't JS-verified.
_JS_GATE_MARKER = "/httpservice/retry/enablejs"

# Generic marketplace prefixes Lens prepends in aria-labels (NOT real brands, so
# strip them); brand boutiques like "Marine Layer" are left intact on purpose.
_MARKETPLACE_PREFIX = re.compile(
    r"^(Amazon\.com|www\.[^\s]+|eBay|Walmart|Target|AliExpress|Etsy|SHEIN|Google Shopping)\s+",
    re.IGNORECASE,
)
_JUNK_EXACT = {"read more", "view related links", "opens in new tab", "see more",
               "shop now", "related links", "view product", "product", ""}
_JUNK_CONTAINS = ("new arrivals & restocks", "let us know your")

# A bare merchant host, optionally with Lens' "(+2)" more-sellers badge and a
# dash, ahead of the actual product name ("thepremierstore.com (+1) - Killshot
# 2"). _MARKETPLACE_PREFIX only knows the named marketplaces and www.-prefixed
# hosts, so a plain shop domain used to survive into the title.
_DOMAIN_PREFIX = re.compile(
    r"^(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}"
    r"(?:\s*\(\+\d+\))?\s*[-–—:]?\s+",
    re.IGNORECASE,
)
# The whole title is just a hostname -- a merchant label, not a product name.
_BARE_DOMAIN = re.compile(
    r"^(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}$", re.IGNORECASE
)
# Google's result breadcrumb ("... Nike https://www.nike.com > Shoes") gets
# flattened into the aria-label; everything from the URL on is chrome, not title.
_EMBEDDED_URL = re.compile(r"\s*(?:https?://|www\.)\S*.*$", re.IGNORECASE)

_browser_lock = threading.Lock()
_cookie_io_lock = threading.Lock()
_VERCEL_PROXY_HEADER = "x-glens-proxy-upstream"
_VERCEL_PROXY_BODY_LIMIT = 4 * 1024 * 1024


class LensRateLimitError(RuntimeError):
    """Google Lens explicitly refused a request with HTTP 429."""

    def __init__(self, retry_after: str | None = None):
        super().__init__("Google Lens rate limited the request.")
        self.retry_after = retry_after


class LensBotChallengeError(RuntimeError):
    """Google Lens intercepted the request with a bot/CAPTCHA challenge."""

    def __init__(self, challenge_url: str | None = None, reason: str = "bot_challenge"):
        super().__init__("Google Lens presented a bot challenge.")
        self.challenge_url = challenge_url
        self.reason = reason


class LensProxyError(RuntimeError):
    """A configured proxy failed before it returned a usable Lens response."""

    def __init__(self, code: str, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


class _VercelProxyAdapter(requests.adapters.BaseAdapter):
    """Route one requests session's Google Lens traffic through a Vercel handler."""

    def __init__(self, proxy_url: str, secret: str):
        self.proxy_url = proxy_url
        self.secret = secret
        self.transport = requests.Session()
        self.transport.trust_env = False

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        try:
            target = urlparse(request.url)
            # Accessing .port validates malformed port numbers before forwarding.
            _ = target.port
            if (target.scheme not in {"http", "https"} or not target.hostname
                    or target.username or target.password):
                raise LensProxyError(
                    "proxy_target_rejected",
                    "Proxy target must be an HTTP(S) URL.",
                )
            body = request.body
            if body is not None and not isinstance(body, bytes):
                raise LensProxyError("proxy_request_invalid", "Proxy request body must be bytes.")
            if body is not None and len(body) > _VERCEL_PROXY_BODY_LIMIT:
                raise LensProxyError("proxy_request_too_large",
                                     "The request exceeds the Vercel proxy size limit.")

            headers = {
                "Authorization": f"Bearer {self.secret}",
                "X-Glens-Target-URL": request.url,
                "X-Glens-Target-Method": request.method,
            }
            forwarded_headers = (
                "accept", "accept-language", "content-type", "cookie", "origin", "referer",
                "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
                "sec-fetch-mode", "sec-fetch-site", "upgrade-insecure-requests", "user-agent",
            )
            for name in forwarded_headers:
                value = request.headers.get(name)
                if value is not None:
                    headers[f"X-Glens-Forward-{name}"] = value

            response = self.transport.post(self.proxy_url, data=body, headers=headers,
                                           timeout=timeout or 30, allow_redirects=False)
        except LensProxyError:
            raise
        except ValueError as exc:
            raise LensProxyError("proxy_target_rejected",
                                 "Proxy target must be a valid HTTP(S) URL.") from exc
        except requests.RequestException as exc:
            raise LensProxyError("proxy_unavailable", "The Vercel proxy request failed.") from exc

        if response.headers.get(_VERCEL_PROXY_HEADER) != "1":
            code = response.headers.get("x-glens-proxy-error", "proxy_unavailable")
            raise LensProxyError(code, "The Vercel proxy could not forward the request.",
                                 response.status_code)

        result = requests.Response()
        result.status_code = response.status_code
        result.reason = response.reason
        result.url = request.url
        result.request = request
        result.headers = response.headers.copy()
        result._content = response.content
        result._content_consumed = True
        # requests.Session extracts cookies from response.raw rather than from
        # response.headers. Recreate the small part of urllib3's response shape
        # it needs so Google's Set-Cookie survives the upload redirect.
        raw_headers = Message()
        raw = getattr(response, "raw", None)
        if raw is not None:
            for cookie in raw.headers.getlist("Set-Cookie"):
                raw_headers.add_header("Set-Cookie", cookie)
        result.raw = SimpleNamespace(
            _original_response=SimpleNamespace(msg=raw_headers) if raw_headers else None
        )
        return result

    def close(self):
        self.transport.close()


@dataclass
class Match:
    """One external product/result link harvested from the Lens page."""
    title: str
    url: str
    domain: str


@dataclass
class LensResult:
    """The outcome of one Lens pass.

    ``matches`` are external merchant/result links; ``titles`` are the
    frequency-ranked product names (the exact match repeats across tiles, so it
    rises to the top). ``raw_anchors`` is every anchor we scraped, unfiltered, for
    callers that want to mine it themselves. ``results_url`` is the internal
    Google results URL the anchors came from; it's tied to the browser-minted
    session (it carries a ``vsrid``/``gsessionid``), so it only renders for a
    client replaying that session's cookies. You can't reopen it in a normal
    browser, so keep it for logging, not for sharing.
    """
    top_title: str | None
    titles: list[str]
    matches: list[Match]
    raw_anchors: list[dict] = field(default_factory=list)
    results_url: str | None = None

    def __bool__(self) -> bool:
        return bool(self.matches or self.titles)

    def __str__(self) -> str:
        lines = [f"top title: {self.top_title or '(none)'}"]
        for m in self.matches[:5]:
            lines.append(f"  [{m.domain}] {m.title}")
        hidden = len(self.matches) - 5
        if hidden > 0:
            lines.append(f"  ... and {hidden} more match(es)")
        return "\n".join(lines)

    def to_dict(self, *, raw_anchors: bool = False) -> dict:
        """JSON-ready dict of the result (``raw_anchors=True`` to include them)."""
        data = asdict(self)
        if not raw_anchors:
            data.pop("raw_anchors", None)
        return data


# Google's own hosts, matched on LABEL boundaries: ``google.<tld>`` and
# ``google.<tld>.<tld>`` (google.com, google.de, google.co.uk, google.com.br)
# plus any subdomain of those, and the gstatic CDNs. Boundaries matter in both
# directions -- a bare substring scan reads the merchant host "notgoogle.com" as
# Google's and drops a real match, while missing google.de entirely because it
# only ever looked for "google.com"/"google.co".
_GOOGLE_HOST = re.compile(r"(^|\.)(google(\.[a-z]{2,3}){1,2}|gstatic(\.[a-z]{2,3}){1,2})$")


def _is_google_host(host: str) -> bool:
    """True when a bare hostname (or a cookie's domain attribute) is Google's.

    The single place that answers "is this Google's?", so link classification and
    cookie-jar minting can't drift apart. A leading dot is a cookie-domain thing
    (``.google.com``), not part of the name, so strip it first.
    """
    h = (host or "").lower().strip().lstrip(".")
    if not h:
        return False
    return (
        h == "google" or h.endswith(".google")   # the .google gTLD
        or bool(_GOOGLE_HOST.search(h))          # google.<tld> + subdomains, gstatic
    )


def _is_external(href: str) -> bool:
    """True for a link that leaves Google.

    Judged by the HOST, with one deliberate exception. A merchant URL that
    merely carries a Google token in its path or query (an affiliate tag like
    ``?utm_source=google.com``, a ``ved`` param) is external and must not be
    dropped. But Google's own outbound *wrapper* is external too: the host is
    google.com while the link itself goes to a merchant, and treating it as
    Google's own drops every product tile on the page (see
    ``_is_google_redirect``).
    """
    h = (href or "").lower()
    if "://" not in h:
        return False
    host = _domain_of(h)  # hostname, lowercased, leading 'www.' stripped
    if not host:
        return False
    if not _is_google_host(host):
        return True
    return _is_google_redirect(href)


# Paths of Google's outbound link wrappers. ``/url`` is the classic one and
# carries its target in the clear; ``/goto`` is what Lens result tiles use now
# and carries an ENCRYPTED blob instead (verified live 2026-09).
_REDIRECT_PATHS = frozenset({"/goto", "/url"})
# Google rate-limits wrapper lookups, so resolve selected links serially.
_RESOLVE_TIMEOUT = 15
_RESOLVE_INTERVAL_SECONDS = 2.0


def _is_google_redirect(href: str) -> bool:
    """True when a URL is Google's own wrapper around an outbound link.

    Recognised by host AND path AND the presence of a target parameter, so a
    Google page that merely has ``url=`` in its query (or a merchant with its
    own ``/goto`` route) isn't mistaken for one.
    """
    if "://" not in (href or ""):
        return False
    try:
        parts = urlparse(href)
    except Exception:
        return False
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not _is_google_host(host):
        return False
    if parts.path.rstrip("/") not in _REDIRECT_PATHS:
        return False
    try:
        params = parse_qs(parts.query)
    except Exception:
        return False
    return bool(params.get("url") or params.get("q"))


def _redirect_target(href: str) -> str | None:
    """The plain URL inside a wrapper, when it carries one.

    ``/url?q=https://merchant...`` hands the target over in the clear, so it
    costs nothing to unwrap. ``/goto?url=<blob>`` doesn't -- the blob is
    encrypted, and only ``_resolve_google_redirects`` can turn it into a URL.
    """
    try:
        params = parse_qs(urlparse(href).query)
    except Exception:
        return None
    target = (params.get("q") or params.get("url") or [""])[0]
    return target if target.startswith(("http://", "https://")) else None


def _domain_of(href: str) -> str:
    """Bare hostname of a URL, lowercased, with a leading ``www.`` dropped.

    Deliberately ``hostname`` and not ``netloc``: netloc keeps the port and any
    userinfo, and both wreck the Google check downstream, which anchors on the
    end of the host. ``https://www.google.com:443/url?q=...`` would read as the
    merchant host "google.com:443" -- external -- so its wrapper never gets
    unwrapped and Google's own link is served up as a product match. It also
    kept the port/userinfo in the user-facing ``Match.domain``.
    """
    try:
        return (urlparse(href).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


# Path of the upload endpoint, so a POST that never redirected can be told from
# the results page it was supposed to land on.
_UPLOAD_PATH = urlparse(_UPLOAD_ENDPOINT).path.rstrip("/")


_BOT_CHALLENGE_HTML_MARKERS = (
    'id="captcha-form"',
    "id='captcha-form'",
    'class="g-recaptcha"',
    "class='g-recaptcha'",
    "our systems have detected unusual traffic",
    "detected unusual traffic from your computer",
    "google.com/sorry/index",
)


def _is_bot_challenge(url: str = "", html: str = "") -> bool:
    """True when Google redirected to /sorry or served a CAPTCHA challenge page."""
    if url and "://" in url:
        try:
            parts = urlparse(url)
            host = (parts.hostname or "").lower().removeprefix("www.")
            if _is_google_host(host) and (
                host.startswith("sorry.") or parts.path.startswith("/sorry")
            ):
                return True
        except Exception:
            pass
    if html:
        low = html.lower()
        if any(marker in low for marker in _BOT_CHALLENGE_HTML_MARKERS):
            return True
    return False


def _looks_like_results_url(url: str) -> bool:
    """True when a URL is the Lens results page both backends are waiting for.

    The single place that answers "did the upload land?", judged by HOST and
    PATH rather than by scanning the whole URL for "lens". A bare substring scan
    got this wrong in both directions:

      * the upload endpoint is itself on ``lens.google.com``, so an upload that
        never redirected read as a results page -- and the req backend went on to
        parse the upload response body as if it were results;
      * a consent/sorry interstitial carries the real target in a query param
        (``consent.google.com/m?continue=https://lens.google.com/...``), so it
        matched too, ending the browser's address-bar poll on the wrong hop.
    """
    if not url or "://" not in url:
        return False
    try:
        parts = urlparse(url)
    except Exception:
        return False
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not _is_google_host(host):
        return False
    # Interstitials are hops, not the destination.
    if host.startswith("consent.") or parts.path.startswith("/sorry"):
        return False
    if "udm=26" in (parts.query or ""):
        return True
    # A lens.google.com results page -- but not the endpoint we just posted to.
    return host == "lens.google.com" and parts.path.rstrip("/") != _UPLOAD_PATH


# How many leading words to consider when matching a merchant's display name
# against its domain ("Sneaker Politics" -> sneakerpolitics.com).
_MERCHANT_PREFIX_WORDS = 4


def _strip_merchant_prefix(title: str, domain: str) -> str:
    """Drop the merchant's display name from the front of a Lens label.

    Lens prefixes every tile label with the seller. It used to use the bare
    HOST ("thepremierstore.com (+1) - Killshot 2"), which ``_DOMAIN_PREFIX``
    strips; it now uses the display NAME instead ("Foot Locker Nike Dunk Low
    Retro - Men's"), which no pattern can recognise on its own -- "Foot Locker"
    is indistinguishable from a product's opening words.

    The domain we resolved the link to settles it: strip the leading words only
    when they spell out that domain ("Foot Locker" -> footlocker.com, "Macy's"
    -> macys.com). A merchant whose name doesn't match its domain (si.com for
    "Sports Illustrated") simply keeps the prefix, as before.

    This is what restores the frequency ranking. With the seller baked into
    every label, each tile for the same shoe cleaned to a *different* string, so
    the exact match no longer repeated and could not rise to the top.
    """
    if not domain or not title:
        return title
    flat = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    targets = {flat(domain), flat(domain.split(".")[0])} - {""}
    if not targets:
        return title
    words = title.split()
    seen = ""
    for count in range(1, min(len(words), _MERCHANT_PREFIX_WORDS) + 1):
        seen += flat(words[count - 1])
        if seen in targets:
            # Never let the merchant's name eat the whole label: a tile whose
            # title really is just the seller keeps it (_is_domain_only screens
            # those out of the ranking downstream).
            rest = " ".join(words[count:]).strip(" -–—:|·")
            return rest or title
    return title


def _clean_title(text: str, domain: str = "") -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    for sep in (" | ", " – ", " — "):  # merchant usually follows; keep the product part
        if sep in t:
            t = t.split(sep)[0].strip()
    t = _MARKETPLACE_PREFIX.sub("", t)
    t = _DOMAIN_PREFIX.sub("", t)
    t = _strip_merchant_prefix(t, domain)
    t = re.sub(r"\b(Opens in new tab|Out of stock|In stock|Read more|View related links)\b.*$",
               "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\d\.\d\s*\(\d[\d,]*\).*$", "", t)  # trailing rating like 4.7(20)
    t = _EMBEDDED_URL.sub("", t)                        # breadcrumb URL tail
    t = re.sub(r"\s*[·|›»].*$", "", t)                  # leftover separators
    return t.strip(" -–—|·›».")


def _is_junk_title(t: str) -> bool:
    low = t.lower().strip()
    return low in _JUNK_EXACT or any(j in low for j in _JUNK_CONTAINS)


def _is_domain_only(t: str) -> bool:
    """True when a cleaned title is nothing but a merchant host ("Amazon.com").

    Only ``_to_titles`` screens on this. It's useless as a *product name* and
    used to pollute the frequency ranking (enough same-merchant tiles and a bare
    domain outranks the real product), but the anchor it came from is still a
    genuine merchant link, so ``_to_matches`` keeps it -- the parser favours
    recall, and ``Match`` carries the URL that actually matters.
    """
    return bool(_BARE_DOMAIN.match(t.strip()))


def _download_image(url: str) -> bytes | None:
    """Fetch an image URL so callers can pass links straight to search().

    A soft-404 (a host answering 200 with an HTML "not found" page) is rejected
    here rather than uploaded to Lens as a JPEG: Google dutifully finds nothing
    in it, and the caller gets a bare "no results" for what is really a dead
    link. Only an unmistakably non-image body is refused -- plenty of CDNs serve
    real images as ``application/octet-stream`` or with no type at all.
    """
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": _REQ_UA_FALLBACK})
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type.startswith("text/") or content_type in {
            "application/json", "application/xhtml+xml", "application/xml",
        }:
            logger.warning("glens: %s served %s, not an image; ignoring it.", url, content_type)
            return None
        return resp.content or None
    except requests.RequestException as exc:
        logger.warning("glens: could not download image %s (%s)", url, exc)
        return None


def _tiny_png() -> bytes:
    """A synthetic 16x16 grey PNG (pure stdlib) used by warmup() when the
    caller doesn't supply an image. Not a Google asset; generated locally."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)   # 16x16, 8-bit RGB
    raw = b"".join(b"\x00" + bytes([120, 120, 120] * 16) for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Locale note (tested live, 2026-07): Google rejects hl/gl overrides here.
# Editing the session-signed results URL gets a 403, and so does putting the
# params on the upload request. The only lever that works is the Accept-Language
# header (see _req_session); country/market always follows your IP.


# Opt-in result cache: sha256(image)+lang -> harvested anchors, plus L2 dHash
# near-duplicate lookup. Shaping (max_matches/max_titles) is applied per call.
def _cache_key(frame_bytes: bytes, lang: str | None) -> str:
    return _cache_mod.cache_key(frame_bytes, lang)


def _cache_read(key: str, ttl: int) -> tuple[list[dict], str | None] | None:
    return _cache_mod.cache_read(key, ttl, cache_dir=_default_cache_dir())


def _cache_read_dhash(
    dhash: int | None,
    lang: str | None,
    ttl: int,
) -> tuple[list[dict], str | None] | None:
    return _cache_mod.cache_read_dhash(dhash, lang, ttl, cache_dir=_default_cache_dir())


def _cache_write(
    key: str,
    items: list[dict],
    results_url: str | None,
    *,
    dhash: int | None = None,
    lang: str | None = None,
) -> None:
    _cache_mod.cache_write(
        key,
        items,
        results_url,
        dhash=dhash,
        lang=lang,
        cache_dir=_default_cache_dir(),
    )


def _discard(path: Path | None) -> None:
    """Best-effort unlink of a temp file we failed to publish."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _save_cookie_jar(driver, cookie_file: Path) -> None:
    """Persist the browser's google.com cookies + UA so the req backend can reuse
    the JS-verified session. Best-effort; called after every successful browser run."""
    tmp: Path | None = None
    try:
        cookies = [
            {k: c.get(k) for k in ("name", "value", "domain", "path") if c.get(k) is not None}
            for c in driver.get_cookies()
            # Match on LABEL boundaries, exactly as link classification does. A
            # substring scan for "google" also swept up cookies belonging to
            # unrelated hosts that merely contain the word ("notgoogle.com",
            # "google.com.example.net") and wrote them into our jar.
            if _is_google_host(c.get("domain") or "")
        ]
        if not cookies:
            return
        ua = driver.execute_script("return navigator.userAgent") or _REQ_UA_FALLBACK
        with _cookie_io_lock:
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a crash or a full disk mid-write would otherwise
            # truncate the jar in place, and a half-written jar is unloadable --
            # taking out the working fast path this run was only meant to refresh.
            tmp = cookie_file.with_name(
                f"{cookie_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(
                json.dumps({"user_agent": ua, "cookies": cookies, "minted_at": time.time()}),
                encoding="utf-8",
            )
            tmp.replace(cookie_file)
            tmp = None
        logger.info("glens: saved %d google.com cookie(s) for the req backend.", len(cookies))
    except Exception as exc:
        logger.info("glens: could not save the cookie jar (%s).", exc)
        _discard(tmp)


def _iter_google_cookies(session_cookies) -> list[dict]:
    """Extract google.com cookie dicts from a cookie jar or list."""
    extracted: list[dict] = []
    if session_cookies is None:
        return extracted
    jar = getattr(session_cookies, "jar", session_cookies)
    if isinstance(jar, list):
        for item in jar:
            if (isinstance(item, dict) and isinstance(item.get("name"), str)
                    and item["name"] and isinstance(item.get("value"), str)):
                domain = item.get("domain") or ".google.com"
                if _is_google_host(str(domain)):
                    extracted.append({
                        "name": item["name"],
                        "value": item["value"],
                        "domain": str(domain),
                        "path": str(item.get("path") or "/"),
                    })
        return extracted
    try:
        for cookie in jar:
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if not isinstance(name, str) or not name or not isinstance(value, str):
                continue
            domain = getattr(cookie, "domain", None) or ".google.com"
            path = getattr(cookie, "path", None) or "/"
            if _is_google_host(str(domain)):
                extracted.append({
                    "name": name,
                    "value": value,
                    "domain": str(domain),
                    "path": str(path),
                })
    except TypeError:
        return extracted
    return extracted


def _merge_and_save_cookies(
    cookie_file: Path,
    session_cookies,
    user_agent: str | None = None,
) -> bool:
    """Merge refreshed google.com Set-Cookie values back into ``cookie_file``.

    Only writes to disk when at least one cookie was added or updated, keeping
    the hot path disk-quiet when Google didn't rotate any session cookies.
    """
    incoming = _iter_google_cookies(session_cookies)
    if not incoming:
        return False
    tmp: Path | None = None
    try:
        with _cookie_io_lock:
            existing_jar = _load_cookie_jar(cookie_file) or {}
            existing_list = existing_jar.get("cookies") or []
            existing_map: dict[tuple[str, str, str], dict] = {}
            for c in existing_list:
                key = (c["name"], c.get("domain") or ".google.com", c.get("path") or "/")
                existing_map[key] = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": key[1],
                    "path": key[2],
                }
            merged_map = dict(existing_map)
            for c in incoming:
                key = (c["name"], c["domain"], c["path"])
                merged_map[key] = c
            if merged_map == existing_map:
                return False
            payload = {
                "user_agent": existing_jar.get("user_agent") or user_agent or _REQ_UA_FALLBACK,
                "cookies": list(merged_map.values()),
                "minted_at": existing_jar.get("minted_at") or time.time(),
                "updated_at": time.time(),
            }
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cookie_file.with_name(
                f"{cookie_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(cookie_file)
            tmp = None
        logger.info("glens: rolled %d google.com cookie(s) to %s.", len(merged_map), cookie_file)
        return True
    except Exception as exc:
        logger.info("glens: could not roll cookies to %s (%s).", cookie_file, exc)
        _discard(tmp)
        return False


def _ensure_route_cookie_file(route_cookie: Path, seed_cookie: Path | None) -> Path:
    """Ensure a route-isolated cookie file exists and stays in sync with newer re-mints."""
    if seed_cookie is None or route_cookie == seed_cookie:
        return route_cookie
    seed = _load_cookie_jar(seed_cookie)
    if seed is None:
        return route_cookie
    existing = _load_cookie_jar(route_cookie)
    if existing is not None:
        # Only re-seed if the base cookie jar was freshly re-minted after this route's jar.
        seed_minted = float(seed.get("minted_at") or 0)
        route_minted = float(existing.get("minted_at") or 0)
        if seed_minted <= route_minted:
            return route_cookie
    tmp: Path | None = None
    try:
        with _cookie_io_lock:
            existing = _load_cookie_jar(route_cookie)
            if existing is not None:
                seed_minted = float(seed.get("minted_at") or 0)
                route_minted = float(existing.get("minted_at") or 0)
                if seed_minted <= route_minted:
                    return route_cookie
            route_cookie.parent.mkdir(parents=True, exist_ok=True)
            tmp = route_cookie.with_name(
                f"{route_cookie.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(json.dumps(seed), encoding="utf-8")
            tmp.replace(route_cookie)
            tmp = None
    except Exception as exc:
        logger.info("glens: could not seed route cookie file %s (%s).", route_cookie, exc)
        _discard(tmp)
    return route_cookie


def _load_cookie_jar(cookie_file: Path) -> dict | None:
    try:
        if not cookie_file.exists():
            return None
        jar = json.loads(cookie_file.read_text(encoding="utf-8"))
        if not isinstance(jar, dict):
            return None
        cookies = jar.get("cookies")
        if not isinstance(cookies, list) or not cookies or any(
            not isinstance(cookie, dict)
            or not isinstance(cookie.get("name"), str) or not cookie["name"]
            or not isinstance(cookie.get("value"), str)
            or any(cookie.get(key) is not None and not isinstance(cookie[key], str)
                   for key in ("domain", "path"))
            for cookie in cookies
        ):
            return None
        if jar.get("user_agent") is not None and not isinstance(jar["user_agent"], str):
            return None
        return jar
    except Exception:
        return None


def _jar_stamp(cookie_file: Path) -> tuple | None:
    """Identity of the jar on disk, so a caller can tell a freshly minted jar
    from the leftover it was meant to replace. ``None`` when there's no valid
    jar. Two signals because either can be absent or coarse: ``minted_at`` is
    missing on a hand-copied jar, and filesystem mtimes are low-resolution."""
    jar = _load_cookie_jar(cookie_file)
    if jar is None:
        return None
    try:
        mtime = cookie_file.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (jar.get("minted_at"), mtime)


def _ua_platform(user_agent: str) -> str:
    if "Macintosh" in user_agent:
        return "macOS"
    if "Linux" in user_agent:
        return "Linux"
    return "Windows"


def _load_curl_cffi():
    """Return curl_cffi.requests when installed and not explicitly disabled."""
    raw = os.getenv("GLENS_USE_CURL_CFFI")
    if raw is not None and raw.strip().lower() in {"0", "false", "no", "off"}:
        return None
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests
    except ImportError:
        return None


class _CurlCffiResponse:
    """Thin requests.Response-compatible wrapper around a curl_cffi response."""

    def __init__(self, inner):
        self._inner = inner
        self.status_code = int(getattr(inner, "status_code", 200))
        self.url = str(getattr(inner, "url", "") or "")
        self.text = getattr(inner, "text", "") or ""
        self.headers = requests.structures.CaseInsensitiveDict(
            dict(getattr(inner, "headers", {}) or {})
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            err_resp = requests.Response()
            err_resp.status_code = self.status_code
            err_resp.url = self.url
            err_resp.headers.update(self.headers)
            err_resp._content = self.text.encode("utf-8", errors="replace")
            raise requests.HTTPError(
                f"{self.status_code} error for url: {self.url}",
                response=err_resp,
            )


class _CurlCffiSession:
    """Drop-in requests.Session shim backed by curl_cffi (Chrome BoringSSL + HTTP/2)."""

    def __init__(self, cffi_requests, impersonate: str = "chrome"):
        self._inner = cffi_requests.Session(impersonate=impersonate)
        self.headers = requests.structures.CaseInsensitiveDict()
        self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies: dict[str, str] = {}

    def _sync_cookies_to_inner(self) -> None:
        inner_cookies = getattr(self._inner, "cookies", None)
        if inner_cookies is None:
            return
        for cookie in self.cookies:
            try:
                inner_cookies.set(
                    cookie.name,
                    cookie.value,
                    domain=cookie.domain or ".google.com",
                    path=cookie.path or "/",
                )
            except Exception:
                continue

    def _sync_cookies_from_inner(self) -> None:
        for item in _iter_google_cookies(getattr(self._inner, "cookies", None)):
            try:
                self.cookies.set(
                    item["name"],
                    item["value"],
                    domain=item["domain"],
                    path=item["path"],
                )
            except Exception:
                continue

    def _encode_multipart(self, files: dict, data: dict | None) -> tuple[bytes, str]:
        from urllib3.filepost import encode_multipart_formdata

        fields: list[tuple[str, object]] = []
        for k, v in (data or {}).items():
            fields.append((k, str(v)))
        for k, v in files.items():
            fields.append((k, v))
        return encode_multipart_formdata(fields)

    def _request(self, method: str, url: str, *, files=None, data=None,
                 timeout=30, allow_redirects=True, headers=None):
        self._sync_cookies_to_inner()
        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)
        payload = data
        if files is not None:
            payload, content_type = self._encode_multipart(files, data)
            req_headers["Content-Type"] = content_type
        kwargs: dict = {
            "headers": req_headers,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
        }
        if payload is not None:
            kwargs["data"] = payload
        if self.proxies:
            kwargs["proxies"] = self.proxies
        try:
            resp = getattr(self._inner, method)(url, **kwargs)
        except requests.RequestException:
            raise
        except Exception as exc:
            raise requests.RequestException(str(exc)) from exc
        self._sync_cookies_from_inner()
        return _CurlCffiResponse(resp)

    def post(self, url: str, *, files=None, data=None, timeout=30,
             allow_redirects=True, headers=None):
        return self._request(
            "post", url, files=files, data=data,
            timeout=timeout, allow_redirects=allow_redirects, headers=headers,
        )

    def get(self, url: str, *, timeout=30, allow_redirects=True, headers=None):
        return self._request(
            "get", url, timeout=timeout,
            allow_redirects=allow_redirects, headers=headers,
        )

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:
            pass


def _req_session(
    jar: dict,
    lang: str | None = None,
    proxy: str | None = None,
    *,
    proxy_url: str | None = None,
    proxy_secret: str | None = None,
    local_proxy: str | None = None,
):
    if proxy is not None:
        session = requests.Session()
        if proxy != "vercel":
            session.close()
            raise ValueError("proxy must be None or 'vercel'")
        resolved_url = (proxy_url if proxy_url is not None
                        else os.getenv("GLENS_VERCEL_PROXY_URL", "")).strip()
        resolved_secret = (proxy_secret if proxy_secret is not None
                           else os.getenv("GLENS_VERCEL_PROXY_SECRET", ""))
        if not resolved_url or not resolved_secret:
            session.close()
            raise LensProxyError("proxy_not_configured",
                                 "Vercel proxy URL and secret must be configured.")
        parsed_proxy_url = urlparse(resolved_url)
        if (parsed_proxy_url.scheme != "https" or not parsed_proxy_url.hostname
                or parsed_proxy_url.username or parsed_proxy_url.password):
            session.close()
            raise LensProxyError("proxy_config_invalid",
                                 "Vercel proxy URL must be an HTTPS URL.")
        adapter = _VercelProxyAdapter(resolved_url, resolved_secret)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    else:
        cffi = _load_curl_cffi()
        session = _CurlCffiSession(cffi) if cffi is not None else requests.Session()
        effective_proxy = (local_proxy or os.getenv("GLENS_PROXY", "").strip() or None)
        if effective_proxy:
            session.proxies.update({"http": effective_proxy, "https": effective_proxy})
    user_agent = jar.get("user_agent") or _REQ_UA_FALLBACK
    # Google fingerprints more than cookies: a bare header set gets the JS-gate
    # shell even with a fresh jar, while the browser's full header surface gets
    # the server-rendered results (verified live 2026-07). Mirror Chrome.
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": f"{lang},en;q=0.8" if lang else "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Origin": "https://www.google.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
    })
    major = re.search(r"Chrome/(\d+)", user_agent)
    if major:
        version = major.group(1)
        session.headers.update({
            "sec-ch-ua": f'"Google Chrome";v="{version}", "Chromium";v="{version}", '
                         f'"Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{_ua_platform(user_agent)}"',
        })
    for cookie in jar.get("cookies", []):
        try:
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain") or ".google.com",
                path=cookie.get("path") or "/",
            )
        except Exception:
            continue
    # Consent opt-out; harmless if the jar already carries one.
    if "SOCS" not in session.cookies:
        session.cookies.set("SOCS", "CAI", domain=".google.com", path="/")
    return session


def _unwrap_google_redirect(href: str) -> str:
    """``google.com/url?q=<target>`` -> ``<target>``, when the target is in the clear.

    Google wraps merchant links in the raw HTML *and* in the rendered DOM, so
    both harvests run hrefs through here. Only Google's own wrapper is unwrapped
    (``_is_google_redirect`` checks the host), so a merchant that happens to have
    its own ``/url?q=`` route keeps its link. An encrypted ``/goto`` wrapper
    comes back unchanged for ``_resolve_google_redirects`` to deal with.
    """
    if not _is_google_redirect(href):
        return href
    return _redirect_target(href) or href


def _resolve_google_redirects(
    items: list[dict],
    cookie_file: Path | None = None,
    selected_hrefs: set[str] | None = None,
) -> None:
    """Rewrite Google's encrypted outbound wrappers to the URLs they point at.

    Google used to hand out ``google.com/url?q=<merchant url>``, which unwraps
    offline. Lens result tiles now use ``google.com/goto?url=<blob>``, where the
    blob is encrypted and the merchant URL appears nowhere in the page — not in
    the href, not in the ``ping`` attribute. Asking is the only way to learn it:
    the wrapper answers ``302`` with the merchant URL in ``Location``. We read
    that header and stop, so the merchant's own site is never contacted.

    Reuses the saved Google session when available, since current wrappers may
    need its cookies and browser headers. Without a valid jar, it falls back to
    a basic request. Best-effort and in place: an anchor we can't resolve keeps
    its wrapper URL, which may remain usable while the Google session is active,
    and ``_is_external`` already counts it as a result either way.
    """
    pending: dict[str, list[dict]] = {}
    for item in items:
        href = item.get("href") or ""
        if selected_hrefs is not None and href not in selected_hrefs:
            continue
        # Only the encrypted ones need the network; /url?q= already unwrapped.
        if _is_google_redirect(href) and not _redirect_target(href):
            pending.setdefault(href, []).append(item)
    if not pending:
        return

    jar = _load_cookie_jar(cookie_file) if cookie_file else None
    if jar:
        session = _req_session(jar)
    else:
        session = requests.Session()
        session.headers.update({"User-Agent": _REQ_UA_FALLBACK})

    def resolve(href: str) -> tuple[str, str]:
        try:
            resp = session.get(href, timeout=_RESOLVE_TIMEOUT, allow_redirects=False)
            target = resp.headers.get("Location", "")
            return href, target if target.startswith(("http://", "https://")) else ""
        except requests.RequestException:
            return href, ""

    try:
        resolved = {}
        for index, href in enumerate(pending):
            resolved_href, target = resolve(href)
            resolved[resolved_href] = target
            if index + 1 < len(pending):
                time.sleep(_RESOLVE_INTERVAL_SECONDS)
    except Exception as exc:
        logger.info("glens: could not resolve Google's link wrappers (%s).", exc)
        return
    finally:
        session.close()

    done = 0
    for href, anchors in pending.items():
        target = resolved.get(href) or ""
        if not target:
            continue
        for item in anchors:
            item["href"] = target
        done += 1
    logger.info("glens: resolved %d/%d Google link wrapper(s).", done, len(pending))


class _AnchorParser(HTMLParser):
    """Read actual HTML attributes and text nodes, with the browser's label fallbacks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.anchor: dict | None = None
        self.text: list[str] = []
        self.alt: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._finish_anchor()
            attributes = dict(attrs)
            if attributes.get("href"):
                self.anchor = attributes
        elif tag == "img" and self.anchor is not None and self.alt is None:
            self.alt = dict(attrs).get("alt") or ""

    def handle_data(self, data):
        if self.anchor is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._finish_anchor()

    def _finish_anchor(self):
        if self.anchor is not None:
            norm = lambda s: re.sub(r"\s+", " ", s or "").strip()  # noqa: E731
            text = (norm(self.anchor.get("aria-label"))
                    or norm(" ".join(self.text)) or norm(self.alt))
            try:
                href = _unwrap_google_redirect(
                    urljoin("https://www.google.com/", self.anchor["href"])
                )
            except ValueError:
                pass  # a malformed link must not discard the rest of the page
            else:
                self.items.append({"href": href, "text": text})
        self.anchor = None
        self.text = []
        self.alt = None


def _parse_anchors_from_html(html: str) -> list[dict]:
    """Server-rendered results -> the browser harvest's [{href, text}] shape.

    HTMLParser handles quotes inside labels, unquoted attributes, entities and
    comments without confusing data-href or JavaScript strings for real links.
    """
    parser = _AnchorParser()
    parser.feed(html)
    parser.close()
    parser._finish_anchor()
    return parser.items


def _search_via_requests(
    frame_bytes: bytes, cookie_file: Path,
    lang: str | None = None,
    proxy: str | None = None,
    *,
    proxy_url: str | None = None,
    proxy_secret: str | None = None,
    local_proxy: str | None = None,
) -> tuple[list[dict] | None, str | None]:
    """Both Lens legs over plain HTTP, riding the browser-minted cookie jar.

    Returns ``(items, results_url)``: the scraped anchor items, or ``(None, ...)``
    when the caller should fall back to the browser (no jar, upload failed, or
    Google served the JS-gate shell, i.e. the jar went stale and a browser run has
    to re-mint it). Transient transport errors get one retry before we give up.
    """
    jar = _load_cookie_jar(cookie_file)
    if jar is None:
        if proxy is not None:
            raise LensProxyError("proxy_cookie_missing",
                                 "A browser-minted Google session is required for proxy searches.")
        logger.info("glens req backend: no cookie jar yet; using the browser to mint one.")
        return None, None
    if proxy is None and local_proxy is None:
        session = _req_session(jar, lang=lang)
    elif proxy is None:
        session = _req_session(jar, lang=lang, local_proxy=local_proxy)
    elif proxy_url is None and proxy_secret is None:
        session = _req_session(jar, lang=lang, proxy=proxy)
    else:
        session = _req_session(
            jar, lang=lang, proxy=proxy,
            proxy_url=proxy_url, proxy_secret=proxy_secret,
        )
    last_exc: Exception | None = None
    # Bound before the loop: the HTTPError handler reports it, and the upload
    # itself can fail before we ever learn where it would have landed.
    results_url: str | None = None
    for attempt in range(1, _REQ_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_REQ_BACKOFF_SECONDS)
        try:
            resp = session.post(
                _UPLOAD_ENDPOINT,
                files={"encoded_image": ("frame.jpg", frame_bytes, "image/jpeg")},
                data={"sbisrc": "Google Chrome"},
                timeout=30,
                allow_redirects=True,
            )
            resp.raise_for_status()
            results_url = resp.url or ""
            html = resp.text or ""
            if _is_bot_challenge(results_url, html):
                logger.warning("glens req backend: Google bot challenge detected (%s).",
                               results_url[:120])
                session.close()
                raise LensBotChallengeError(results_url or None, reason="req_upload_challenge")
            if not _looks_like_results_url(results_url):
                logger.info("glens req upload didn't yield a results URL (%s); falling back.",
                            results_url[:120])
                session.close()
                return None, None
            items = _parse_anchors_from_html(html)
            external = [i for i in items if _is_external(i["href"])]
            # Judge the page by its CONTENT, not the enable-js marker: healthy
            # server-rendered pages carry that noscript link too. Some responses
            # redirect with an empty body and need an explicit GET.
            if len(external) < 3:
                resp = session.get(results_url, timeout=30)
                resp.raise_for_status()
                results_url = resp.url or results_url
                html = resp.text or ""
                if _is_bot_challenge(results_url, html):
                    logger.warning(
                        "glens req backend: Google bot challenge detected on GET (%s).",
                        results_url[:120],
                    )
                    session.close()
                    raise LensBotChallengeError(results_url or None, reason="req_get_challenge")
                items = _parse_anchors_from_html(html)
                external = [i for i in items if _is_external(i["href"])]
            if len(external) < 3:
                # A verified session always renders merchant tiles; near-zero
                # external anchors means a gated/degraded page, so fall back and
                # let the browser re-mint the jar.
                if _JS_GATE_MARKER in html:
                    logger.info("glens req backend: JS-gate shell served (stale/weak "
                                "jar); falling back to the browser to re-mint it.")
                else:
                    logger.info("glens req backend: only %d external anchor(s); "
                                "falling back.", len(external))
                session.close()
                return None, results_url
            _merge_and_save_cookies(
                cookie_file, getattr(session, "cookies", None), jar.get("user_agent")
            )
            logger.info("glens req backend: harvested %d anchor(s) without a browser.",
                        len(items))
            session.close()
            return items, results_url
        except LensBotChallengeError:
            raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                session.close()
                if proxy is not None and exc.response.headers.get(_VERCEL_PROXY_HEADER) != "1":
                    raise LensProxyError(
                        "proxy_upstream_unmarked",
                        "The proxy returned an unverified 429 response.",
                        429,
                    ) from exc
                raise LensRateLimitError(exc.response.headers.get("Retry-After")) from exc
            if exc.response is not None and _is_bot_challenge(
                getattr(exc.response, "url", "") or "",
                getattr(exc.response, "text", "") or "",
            ):
                session.close()
                raise LensBotChallengeError(
                    getattr(exc.response, "url", None),
                    reason=f"http_{exc.response.status_code}_challenge",
                ) from exc
            if proxy is not None:
                session.close()
                status_code = exc.response.status_code if exc.response is not None else None
                raise LensProxyError(
                    "upstream_rejected",
                    "Google Lens rejected the proxied request.",
                    status_code,
                ) from exc
            # A hard status is deterministic, not transient: retrying re-uploads
            # the image to an endpoint that just refused it. Fall back
            # now so the browser can re-mint the jar.
            logger.info("glens req backend: HTTP error (%s); falling back to the browser.", exc)
            session.close()
            return None, results_url
        except LensProxyError:
            session.close()
            raise
        except requests.RequestException as exc:
            if proxy is not None:
                session.close()
                raise LensProxyError("proxy_unavailable",
                                     "The proxied Lens request failed.") from exc
            last_exc = exc
            logger.info("glens req backend attempt %d/%d failed (%s).",
                        attempt, _REQ_ATTEMPTS, exc)
    logger.info("glens req backend gave up after %d attempts (%s); falling back to the browser.",
                _REQ_ATTEMPTS, last_exc)
    session.close()
    if proxy is not None:
        raise LensProxyError("proxy_lookup_failed",
                             "Vercel proxy did not return usable Lens results.")
    return None, None


def _dismiss_consent(driver) -> None:
    """Best-effort click-through of Google's cookie/consent interstitial."""
    from selenium.webdriver.common.by import By
    for xpath in (
        "//button[contains(., 'Accept all')]",
        "//button[contains(., 'I agree')]",
        "//div[@role='button'][contains(., 'Accept all')]",
    ):
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
            if buttons:
                buttons[0].click()
                time.sleep(1.0)
                return
        except Exception:
            continue


def _upload_to_google(driver, frame_bytes: bytes, result_timeout: int,
                      endpoint: str = _UPLOAD_ENDPOINT) -> str | None:
    """UPLOAD step from inside the browser (cookies/session match the page we'll
    render). Returns the ``&udm=26`` results URL, or None.

    Submits a real multipart form so the POST is a top-level *navigation*. An
    in-page ``fetch`` can't do this job: the endpoint is on lens.google.com while
    Google lands the session on www.google.com, so the upload is cross-origin and
    the response comes back CORS-opaque ("TypeError: Failed to fetch").
    Navigations aren't subject to CORS, and the redirect target is then simply
    the browser's own URL.
    """
    b64 = base64.b64encode(frame_bytes).decode()
    try:
        before = driver.current_url or ""
        driver.execute_script(
            r"""
            const b64 = arguments[0], endpoint = arguments[1];
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = endpoint;
            form.enctype = 'multipart/form-data';
            const file = document.createElement('input');
            file.type = 'file';
            file.name = 'encoded_image';
            // A file input's .files is read-only; DataTransfer is the only way
            // to hand it bytes we generated rather than a user's pick.
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], 'frame.jpg', {type: 'image/jpeg'}));
            file.files = dt.files;
            form.appendChild(file);
            const src = document.createElement('input');
            src.type = 'hidden';
            src.name = 'sbisrc';
            src.value = 'Google Chrome';
            form.appendChild(src);
            document.body.appendChild(form);
            form.submit();
            """,
            b64, endpoint,
        )
    except Exception as exc:
        logger.info("glens upload failed: %s", exc)
        return None

    # Poll the address bar for the redirect target. Keep the newest changed URL
    # but stop early on one that actually looks like a results page, so a slow
    # intermediate hop can't be mistaken for the destination.
    settled = ""
    deadline = time.time() + result_timeout + _UPLOAD_GRACE_SECONDS
    while time.time() < deadline:
        try:
            current = driver.current_url or ""
        except Exception:
            current = ""
        if current and _is_bot_challenge(current):
            logger.warning("glens upload: Google bot challenge detected (%s).", current[:120])
            raise LensBotChallengeError(current, reason="browser_upload_challenge")
        if current and current != before and not current.startswith(endpoint):
            settled = current
            if _looks_like_results_url(current):
                break
        time.sleep(0.5)
    if not settled:
        logger.info("glens upload returned no results URL (still on %s)", before[:120])
        return None
    return settled


def _harvest_anchors(driver) -> list[dict]:
    """Return [{href, text}] for every result anchor via one JS round-trip."""
    try:
        items = driver.execute_script(
            r"""
            const norm = s => (s||'').replace(/\s+/g,' ').trim();
            // Join the anchor's text nodes with a SPACE. This mirrors the
            // raw-HTML harvest's tag -> " " substitution, so both backends hand
            // the same text downstream. Neither DOM shortcut does that job:
            // innerText reports only what's *rendered*, and Lens tiles clip
            // theirs ("for from Nike" instead of the product name), while
            // textContent runs adjacent elements together with nothing between
            // them ("Foot LockerNike Dunk Low Retro").
            const spacedText = el => {
                const parts = [];
                const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                while (walk.nextNode()) parts.push(walk.currentNode.nodeValue);
                return parts.join(' ');
            };
            // norm() before each fallback: a whitespace-only label is not a
            // label, and taking it literally shadowed the img alt behind it.
            const label = a => norm(a.getAttribute('aria-label'))
                            || norm(spacedText(a))
                            || norm((a.querySelector('img')||{}).alt);
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href || '',
                text: label(a)
            })).filter(x => x.href);
            """
        )
    except Exception as exc:
        logger.info("glens anchor harvest failed: %s", exc)
        return []
    # The rendered DOM carries the same google.com/url?q= wrappers the raw HTML
    # does. Unwrap here, exactly as the req backend does, or _is_external() reads
    # a wrapped merchant link as google.com's own and drops every match the
    # browser pass just found — silently gutting the self-healing fallback.
    for item in items or []:
        item["href"] = _unwrap_google_redirect(item.get("href") or "")
    return items or []


def _render_and_harvest(driver, results_url: str, result_timeout: int, wait_ready) -> list[dict]:
    """RENDER step: load the results URL and wait for product anchors to appear."""
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(results_url)
    try:
        wait_ready(driver)
    except Exception:
        pass
    current_url = getattr(driver, "current_url", "") or ""
    page_source = getattr(driver, "page_source", "") or ""
    if _is_bot_challenge(current_url, page_source):
        logger.warning("glens browser: Google bot challenge detected on results (%s).",
                       current_url[:120])
        raise LensBotChallengeError(current_url or results_url, reason="browser_render_challenge")
    _dismiss_consent(driver)

    def _has_products(drv):
        # Counts wrapped tiles (google.com/url?q=<merchant>) as products too;
        # otherwise a page that wraps every link never satisfies the wait and we
        # burn the whole timeout before harvesting results that were long ready.
        try:
            return drv.execute_script(
                r"""
                // Match Google's hosts on LABEL boundaries, as _is_google_host
                // does. Scanning for the literal "google.com" missed every other
                // Google domain (google.de, google.co.uk, gstatic), so on a
                // non-.com locale the page's own nav/footer links counted as
                // products: the wait was satisfied instantly and we harvested
                // before a single tile had rendered.
                const GOOGLE = /(^|\.)(google|gstatic)(\.[a-z]{2,3}){1,2}$/;
                const isGoogle = h =>
                    GOOGLE.test(h) || h === 'google' || h.endsWith('.google');
                return Array.from(document.querySelectorAll('a[href]')).filter(a => {
                    const h = a.href || '';
                    if (!h.includes('://')) return false;
                    let host = '';
                    try { host = new URL(h).hostname.toLowerCase(); } catch (e) { return false; }
                    if (!isGoogle(host)) return true;
                    // Google's outbound wrappers are products too. /goto carries
                    // an ENCRYPTED target, so testing for "url=http" (the old
                    // /url?q= shape) matched none of them: on a page where every
                    // tile is wrapped the wait was never satisfied and we burned
                    // the whole timeout before harvesting.
                    let path = '';
                    try { path = new URL(h).pathname.replace(/\/+$/, ''); }
                    catch (e) { return false; }
                    if (path !== '/goto' && path !== '/url') return false;
                    return /[?&](url|q)=./.test(h);
                }).length;
                """
            ) > 5
        except Exception:
            return False

    try:
        WebDriverWait(driver, result_timeout).until(_has_products)
    except Exception:
        pass
    time.sleep(2.0)  # let late tiles settle
    return _harvest_anchors(driver)


# A JS-gate/consent shell carries a handful of anchors; a real rendered results
# page carries dozens. Below this, don't trust the page (or its cookies).
_MIN_RENDERED_ANCHORS = 10


def _looks_rendered(items: list[dict] | None) -> bool:
    return bool(items) and len(items) >= _MIN_RENDERED_ANCHORS


def _headed_retry_allowed(driver_factory: Callable) -> bool:
    """Google routinely serves headless Chrome a degraded page. We can retry
    with a visible window, but only for the bundled factory and only when the
    user hasn't pinned GLENS_HEADLESS themselves."""
    if os.getenv("GLENS_HEADLESS") is not None:
        return False
    try:
        from .driver import build_driver
    except Exception:
        return False
    return driver_factory is build_driver


def _browser_pass(frame_bytes: bytes, cookie_file: Path, result_timeout: int,
                  driver_factory: Callable) -> tuple[list[dict] | None, str | None]:
    """One full-browser pass; saves the jar only when the page really rendered."""
    from .driver import wait_for_document_ready

    driver = None
    try:
        driver = driver_factory()
        driver.get(_LANDING_PAGE)
        try:
            wait_for_document_ready(driver)
        except Exception:
            pass
        landing_url = getattr(driver, "current_url", "") or ""
        if _is_bot_challenge(landing_url, getattr(driver, "page_source", "") or ""):
            logger.warning("glens browser: Google bot challenge on landing page (%s).",
                           landing_url[:120])
            raise LensBotChallengeError(landing_url, reason="browser_landing_challenge")
        _dismiss_consent(driver)
        results_url = _upload_to_google(driver, frame_bytes, result_timeout)
        if not results_url:
            return None, None
        items = _render_and_harvest(driver, results_url, result_timeout, wait_for_document_ready)
        if _looks_rendered(items):
            _save_cookie_jar(driver, cookie_file)
        else:
            page = getattr(driver, "current_url", "") or ""
            if _is_bot_challenge(page, getattr(driver, "page_source", "") or ""):
                logger.warning("glens browser: Google bot challenge after render (%s).",
                               page[:120])
                raise LensBotChallengeError(page or results_url, reason="browser_degraded_sorry")
            logger.info("glens browser: page looks degraded (%d anchor(s)); "
                        "not saving the jar.", len(items or []))
        if not _to_matches(items or [], 1) and not _to_titles(items or [], 1):
            # Empty and degraded pages can look alike. Only an explicit visible empty
            # state on a Lens results page establishes a miss without harvested tiles.
            page = getattr(driver, "current_url", "")
            if _looks_like_results_url(page) and driver.execute_script(
                r"""return (document.body.innerText || '').split(/\r?\n/).some(
                    line => /^(no results found|no matches found|no visual matches found)[.!]?$/i
                        .test(line.trim()));"""
            ):
                return [], results_url
        return (items or None), results_url
    except LensBotChallengeError:
        raise
    except ImportError as exc:
        # The [browser] extra isn't installed. This is the most common first-run
        # stumble, so surface it even when logging isn't configured. Other
        # failures (a user driver_factory blowing up, say) stay at INFO below.
        logger.warning("glens: browser backend unavailable: %s", exc)
        return None, None
    except Exception as exc:
        logger.info("glens browser lookup failed: %s", exc)
        return None, None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _search_via_browser(frame_bytes: bytes, cookie_file: Path, result_timeout: int,
                        driver_factory: Callable) -> tuple[list[dict] | None, str | None]:
    """Full-browser flow; also re-mints the req backend's cookie jar.

    Returns ``(items, results_url)``, or ``(None, ...)`` on failure. When headless
    Chrome gets a degraded page (Google gates headless sessions), retries once
    with a visible window (bundled driver only; opt out via GLENS_HEADLESS). The
    retry can only improve the outcome: it's kept when it rendered a real page or
    simply harvested more, never when it did worse. A visible window can't open
    on a display-less box (the documented headless-server case) and Google can
    answer the second upload with an interstitial, so an unconditional overwrite
    would throw away anchors the first pass already had -- turning a degraded but
    usable result into ``None``.
    """
    items, results_url = _browser_pass(frame_bytes, cookie_file, result_timeout,
                                       driver_factory)
    if items != [] and not _looks_rendered(items) and _headed_retry_allowed(driver_factory):
        logger.warning("glens: headless Chrome got a degraded page; retrying once "
                       "with a visible Chrome window.")
        from .driver import build_driver
        retry_items, retry_url = _browser_pass(frame_bytes, cookie_file, result_timeout,
                                               lambda: build_driver(headless=False))
        if (_looks_rendered(retry_items) or len(retry_items or []) > len(items or [])
                or (retry_items == [] and not items)):
            items, results_url = retry_items, retry_url
        else:
            logger.info("glens: the visible-window retry did no better (%d anchor(s)); "
                        "keeping the first pass.", len(retry_items or []))
    return items, results_url


def _to_matches(items: list[dict], max_matches: int) -> list[Match]:
    seen: set[tuple[str, str]] = set()
    out: list[Match] = []
    for item in items:
        if len(out) >= max_matches:
            break
        href = item.get("href", "")
        if not _is_external(href):
            continue
        # A wrapper we couldn't resolve is still a working link, but its host is
        # google.com and reporting that as the merchant would be a lie.
        domain = "" if _is_google_redirect(href) else _domain_of(href)
        title = _clean_title(item.get("text", ""), domain)
        if not title or _is_junk_title(title):
            continue
        # A wrapper we couldn't resolve is still a working link, but its host is
        # google.com and reporting that as the merchant would be a lie.
        key = (domain, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(Match(title=title[:200], url=href, domain=domain))
    return out


def _to_titles(items: list[dict], max_titles: int) -> list[str]:
    """Frequency-ranked product titles (the exact match repeats across tiles, so it
    rises to the top; ties keep Google's relevance order)."""
    cleaned = []
    for item in items:
        href = item.get("href", "")
        if not _is_external(href):
            continue
        title = _clean_title(item.get("text", ""),
                             "" if _is_google_redirect(href) else _domain_of(href))
        if (8 <= len(title) <= 100 and not _is_junk_title(title)
                and not _is_domain_only(title)):
            cleaned.append(title)
    return [title for title, _ in Counter(cleaned).most_common(max_titles)]


def search(
    image: str | os.PathLike | bytes,
    *,
    backend: str | None = None,
    driver_factory: Callable | None = None,
    cookie_file: str | os.PathLike | None = None,
    max_matches: int | None = None,
    max_titles: int | None = None,
    timeout: int | None = None,
    lang: str | None = None,
    use_cache: bool | None = None,
    resolve_links: bool = False,
    proxy: str | None = None,
) -> LensResult | None:
    """Run one Google Lens pass on an image.

    Args:
        image: a path to an image file, an http(s) image URL, or raw image bytes.
        backend: "auto" (default), "req", or "browser".
        driver_factory: zero-arg callable returning a Selenium WebDriver, used by
            the browser backend. Defaults to the bundled undetected-chromedriver
            builder (``glens.driver.build_driver``).
        cookie_file: where the browser-minted cookie jar lives. Defaults to
            ``~/.cache/glens/cookies.json`` (or ``$GLENS_COOKIE_FILE``).
        max_matches / max_titles / timeout: per-call overrides for the env defaults.
        lang: preferred response language ("de", "pt-BR"), sent as the fast
            path's Accept-Language header. Best-effort: Google decides, and the
            country/market always follows your IP (hl/gl URL overrides get
            rejected by this endpoint). Env default GLENS_LANG.
        use_cache: reuse recent results for the same image+locale from the local
            result cache (TTL ``GLENS_CACHE_TTL``, default 24h; dir
            ``GLENS_CACHE_DIR``). Defaults to ``GLENS_CACHE`` (off). Never
            consulted for ``backend="browser"``, since an explicit browser run
            also re-mints the cookie jar and a cache hit must not skip that.
        resolve_links: resolve Google's temporary ``/goto`` wrappers to merchant
            URLs. This uses the saved Google session and is slower, so it defaults
            to False.
        proxy: optionally route the requests backend through the configured
            ``"vercel"`` proxy. Proxy lookups bypass the local cache and never
            fall back to Chrome.

    Returns a ``LensResult`` or ``None`` when the lookup fails. Never raises for
    ordinary network/parse failures; an unknown ``backend`` raises ``ValueError``
    (that's a config bug, not a lookup failure).
    """
    backend = (backend or "").strip().lower() or DEFAULT_BACKEND
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"backend must be one of {_VALID_BACKENDS!r}, not {backend!r}")
    if isinstance(image, bytes):
        frame_bytes = image
    elif isinstance(image, str) and image.lower().startswith(("http://", "https://")):
        maybe_bytes = _download_image(image)
        if maybe_bytes is None:
            return None
        frame_bytes = maybe_bytes
    else:
        try:
            frame_bytes = Path(image).read_bytes()
        except OSError as exc:
            # Missing, unreadable, locked, or a directory: an ordinary failure.
            logger.info("glens: could not read image %s (%s)", image, exc)
            return None

    result = _search_bytes(frame_bytes, backend=backend, driver_factory=driver_factory,
                           cookie_file=cookie_file, max_matches=max_matches, max_titles=max_titles,
                           timeout=timeout, lang=lang, use_cache=use_cache,
                           resolve_links=resolve_links, proxy=proxy)
    return result or None


def _shape_result(
    items: list[dict] | None,
    results_url: str | None,
    *,
    max_matches: int,
    max_titles: int,
    resolve_links: bool = False,
    cookie_path: Path | None = None,
) -> LensResult | None:
    if items is None:
        return None
    if items == []:
        return LensResult(None, [], [], results_url=results_url)

    if resolve_links:
        resolve_cap = min(max_matches, DEFAULT_RESOLVE_MAX) if max_matches > 0 else 0
        selected_hrefs = {match.url for match in _to_matches(items, resolve_cap)}
        _resolve_google_redirects(items, cookie_path, selected_hrefs)

    matches = _to_matches(items, max_matches)
    titles = _to_titles(items, max_titles)
    if not matches and not titles:
        # An unrecognised page cannot establish a miss. Zero caps can also
        # intentionally hide otherwise usable results.
        if not ((max_matches == 0 and _to_matches(items, 1))
                or (max_titles == 0 and _to_titles(items, 1))):
            return None

    logger.info("glens: %d title(s), %d match(es)", len(titles), len(matches))
    return LensResult(
        top_title=titles[0] if titles else None,
        titles=titles,
        matches=matches,
        raw_anchors=items,
        results_url=results_url,
    )


def _search_bytes(
    frame_bytes: bytes, *, backend: str | None = None, driver_factory: Callable | None = None,
    cookie_file: str | os.PathLike | None = None, max_matches: int | None = None,
    max_titles: int | None = None, timeout: int | None = None, lang: str | None = None,
    use_cache: bool | None = None, resolve_links: bool = False,
    raise_on_rate_limit: bool = False, proxy: str | None = None,
    raise_on_proxy_error: bool = False,
    raise_on_bot_challenge: bool = False,
) -> LensResult | None:
    """Return None for inconclusive lookups and an empty LensResult for confirmed misses."""
    backend_setting = backend
    backend = (backend or "").strip().lower() or DEFAULT_BACKEND
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"backend must be one of {_VALID_BACKENDS!r}, not {backend!r}")
    if proxy not in (None, "vercel"):
        raise ValueError("proxy must be None or 'vercel'")
    if proxy is not None and backend == "browser" and backend_setting is not None:
        raise ValueError("proxy searches require the requests backend")
    if proxy is not None:
        # An explicit proxy request uses requests even if the process default is
        # browser; only an explicit per-call browser backend is incompatible.
        backend = "req"
    max_matches = DEFAULT_MAX_MATCHES if max_matches is None else max_matches
    max_titles = DEFAULT_MAX_TITLES if max_titles is None else max_titles
    timeout = DEFAULT_RESULT_TIMEOUT if timeout is None else timeout
    lang = lang or DEFAULT_LANG
    use_cache = DEFAULT_USE_CACHE if use_cache is None else use_cache
    cookie_path = Path(cookie_file) if cookie_file else _default_cookie_file()

    if driver_factory is None:
        from .driver import build_driver
        driver_factory = build_driver

    items: list[dict] | None = None
    results_url: str | None = None
    cache_hit = False
    cache_key = _cache_key(frame_bytes, lang) if use_cache else None
    dhash: int | None = None
    if cache_key and backend != "browser":
        # An explicit browser run is also how the cookie jar gets re-minted;
        # never let a cache hit silently skip that side effect.
        cached = _cache_read(cache_key, DEFAULT_CACHE_TTL)
        if cached is not None:
            items, results_url = cached
            cache_hit = True
            _cache_mod.stats.record_l1_hit()
        else:
            dhash = _cache_mod.compute_dhash(frame_bytes)
            cached_l2 = _cache_read_dhash(dhash, lang, DEFAULT_CACHE_TTL)
            if cached_l2 is not None:
                items, results_url = cached_l2
                cache_hit = True
                _cache_mod.stats.record_l2_hit()
            else:
                _cache_mod.stats.record_miss()
    elif cache_key:
        dhash = _cache_mod.compute_dhash(frame_bytes)

    if items is None and backend in ("auto", "req"):
        try:
            if proxy is None:
                items, results_url = _search_via_requests(frame_bytes, cookie_path, lang)
            else:
                items, results_url = _search_via_requests(frame_bytes, cookie_path, lang,
                                                          proxy=proxy)
        except LensRateLimitError:
            if raise_on_rate_limit:
                raise
            logger.info("glens req backend received an upstream rate limit.")
            return None
        except LensBotChallengeError:
            if raise_on_bot_challenge:
                raise
            logger.warning("glens req backend hit a Google bot challenge; skipping browser retry.")
            return None
        except LensProxyError:
            if raise_on_proxy_error:
                raise
            logger.info("glens proxy search failed.")
            return None
    if items is None and proxy is not None:
        if raise_on_proxy_error:
            raise LensProxyError("proxy_lookup_failed",
                                 "Vercel proxy did not return usable Lens results.")
        return None
    if items is None and backend in ("auto", "browser"):
        # Keep browser use single-filed because it may share a persistent Chrome
        # profile and refresh the cookie jar. Request-backed searches can overlap.
        try:
            with _browser_lock:
                items, results_url = _search_via_browser(frame_bytes, cookie_path, timeout,
                                                         driver_factory)
        except LensBotChallengeError:
            if raise_on_bot_challenge:
                raise
            logger.warning("glens browser backend hit a Google bot challenge.")
            return None
    if items is None:
        return None
    if items == []:
        return LensResult(None, [], [], results_url=results_url)

    result = _shape_result(
        items,
        results_url,
        max_matches=max_matches,
        max_titles=max_titles,
        resolve_links=resolve_links,
        cookie_path=cookie_path,
    )
    if result is not None and cache_key and (not cache_hit or resolve_links):
        _cache_write(cache_key, items, results_url, dhash=dhash, lang=lang)
    return result


def search_many(
    images: list[str | os.PathLike | bytes],
    **kwargs,
) -> list[LensResult | None]:
    """Run :func:`search` over several images, preserving order.

    Keyword arguments are forwarded to each ``search()`` call. This helper runs
    the list sequentially and preserves order; separate ``search()`` calls may
    overlap on the requests backend. A failed lookup yields a ``None`` entry
    instead of aborting the whole batch.
    """
    return [search(image, **kwargs) for image in images]


def status(cookie_file: str | os.PathLike | None = None) -> dict:
    """Health check for automation: is the fast HTTP path ready right now?

    Returns a plain dict (JSON-safe)::

        {
          "cookie_file": str,            # jar location being checked
          "jar_valid": bool,             # a loadable jar with cookies exists
          "jar_age_seconds": float|None, # since the jar was minted
          "browser_extra_installed": bool,
          "req_ready": bool,             # fast path available without Chrome
          "default_backend": str,
          "cache_enabled": bool,
        }

    Typical startup assertion: ``if not glens.status()["req_ready"]:
    glens.warmup()``. Note a *stale* jar can only be detected by a live lookup;
    ``jar_valid`` means present and well-formed.
    """
    cookie_path = Path(cookie_file) if cookie_file else _default_cookie_file()
    jar = _load_cookie_jar(cookie_path)
    age: float | None = None
    if jar is not None:
        minted_at = jar.get("minted_at")
        if minted_at is not None:
            try:
                age = max(0.0, time.time() - float(minted_at))
            except (TypeError, ValueError):
                age = None
    return {
        "cookie_file": str(cookie_path),
        "jar_valid": jar is not None,
        "jar_age_seconds": age,
        "browser_extra_installed":
            importlib.util.find_spec("undetected_chromedriver") is not None,
        "req_ready": jar is not None,
        "default_backend": DEFAULT_BACKEND,
        "cache_enabled": DEFAULT_USE_CACHE,
    }


def warmup(
    image: str | os.PathLike | bytes | None = None,
    *,
    cookie_file: str | os.PathLike | None = None,
    driver_factory: Callable | None = None,
    timeout: int | None = None,
    force: bool = False,
) -> bool:
    """Mint (or refresh) the session jar with one browser run, so subsequent
    lookups run over plain HTTP. The deploy-time counterpart of ``search()``:
    call it at container start / CI setup, then use ``backend="req"`` at runtime.

    Needs the ``[browser]`` extra. No-op (returns True) when a valid jar already
    exists, unless ``force=True``. ``image`` defaults to a tiny synthetic PNG;
    the upload content doesn't matter, only the JS-verified session it creates.

    Returns True when the jar is ready — and for ``force=True``, only when this
    run actually re-minted it: a failed re-mint leaves the old (presumed stale)
    jar on disk, and reporting that as success would tell a deploy check the
    session was refreshed when it wasn't. Never raises for ordinary failures.
    """
    cookie_path = Path(cookie_file) if cookie_file else _default_cookie_file()
    if not force and _load_cookie_jar(cookie_path) is not None:
        return True

    if image is None:
        frame_bytes = _tiny_png()
    elif isinstance(image, bytes):
        frame_bytes = image
    elif isinstance(image, str) and image.lower().startswith(("http://", "https://")):
        maybe = _download_image(image)
        if maybe is None:
            return False
        frame_bytes = maybe
    else:
        try:
            frame_bytes = Path(image).read_bytes()
        except OSError as exc:
            logger.warning("glens: warmup could not read image %s (%s)", image, exc)
            return False

    if driver_factory is None:
        from .driver import build_driver
        driver_factory = build_driver

    # A junk image renders few tiles, so there's no point waiting long: any
    # rendered results page mints the cookies we're after.
    before = _jar_stamp(cookie_path)
    try:
        with _browser_lock:
            _search_via_browser(frame_bytes, cookie_path, timeout or 10, driver_factory)
    except LensBotChallengeError:
        logger.warning("glens: warmup hit a Google bot challenge; skipping retry.")
        return False
    after = _jar_stamp(cookie_path)
    if after is None:
        return False
    if force and after == before:
        # The jar on disk is the one we set out to replace: the browser pass
        # never got a page good enough to save cookies from.
        logger.warning("glens: warmup(force=True) could not re-mint the session jar; "
                       "the existing one was left in place.")
        return False
    return True
