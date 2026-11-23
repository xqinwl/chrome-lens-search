# chrome-lens-search

[![CI](https://github.com/darcodev/chrome-lens-search/actions/workflows/ci.yml/badge.svg)](https://github.com/darcodev/chrome-lens-search/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chrome-lens-search)](https://pypi.org/project/chrome-lens-search/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) | [简体中文](README_zh.md)

> **Unofficial.** Not affiliated with, endorsed by, or sponsored by Google. "Google Lens" is a trademark of Google LLC. This is an independent, community-built client for research and educational use.

**Unofficial Google Lens visual product search over plain HTTP.** Give it an image, get back the products Google Lens sees in it — exact product titles plus external merchant/result links. No SerpApi bill, and none of the OCR-only limitations of the free Lens libraries.

<!-- TODO: add demo screenshot/GIF (real `glens search` output on a real image) -->

## Quickstart

```bash
pip install "chrome-lens-search[browser]"
glens sneaker.jpg
```

That's it. The **first** lookup launches Chrome once to create a reusable Google session — it tries headless first and may briefly show a window if Google blocks the headless attempt. **Every lookup after that is a plain, fast HTTP request** (~2 s), no browser, no Selenium in the hot path.

Or from Python — paths, image URLs, or raw bytes all work:

```python
import glens

result = glens.search("sneaker.jpg")                       # local file
result = glens.search("https://example.com/sneaker.jpg")   # or an image URL

if result:                             # None when the lookup fails
    print(result.top_title)            # "Nike Air Max 90 Men's Shoes"
    for m in result.matches:
        print(f"[{m.domain}] {m.title} -> {m.url}")
```

```
[nike.com]      Nike Air Max 90 Men's Shoes -> https://www.nike.com/t/...
[stockx.com]    Nike Air Max 90 -> https://stockx.com/...
[goat.com]      Air Max 90 -> https://www.goat.com/...
```

## HTTP service and Docker

### 1. Configure `.env` and start

Create a `.env` file in the project root (gitignored):

```env
# Optional Vercel proxy (single URL or comma-separated multi-region URLs)
GLENS_VERCEL_PROXY_URL=https://your-vercel-app.vercel.app/api/lens
GLENS_VERCEL_PROXY_SECRET=your-secret

# Routing, failover, and rate-limit defaults (compose.yaml defaults, tuned for 60+ req/min)
GLENS_PROXY_FALLBACK=1
GLENS_VERCEL_RPM=50
GLENS_LOCAL_RPM=30
GLENS_SLOT_MIN_INTERVAL=0.3

# Optional outbound proxy for local channel + Chromium cookie refreshes (useful on cloud VMs)
# GLENS_PROXY=http://user:pass@proxy-host:port
# GLENS_LOCAL_PROXIES=http://proxy1:8080,http://proxy2:8080

# Optional local-dev only: allow Clash Fake-IP (198.18.0.0/15); keep 0 in production
# GLENS_ALLOW_FAKE_IP=1
```

Start the service and prewarm the session:

```bash
docker compose up -d --build
curl http://localhost:8000/healthz
curl -X POST http://localhost:8000/v1/warmup
curl http://localhost:8000/v1/pool/status
```

Compose builds for the host architecture (Linux AMD64 or ARM64) with Chromium, its matching driver, `curl_cffi`, and a virtual display (`Xvfb`). It publishes port `8000` on localhost (override with `GLENS_PORT=8080`). Other containers on the Compose network can reach `http://glens:8000`. The named volume `glens-data` mounted at `/data` preserves cookies, Chrome profile, result cache, and rotating logs across restarts and rebuilds. On cloud VMs (e.g., DigitalOcean), at least **2 GB RAM** (or 1 GB RAM + 2 GB swap) is recommended for Chromium refreshes.

### 2. Search requests

**Recommended (Vercel-first with automatic local failover / load-sharing):** send JSON with `image_url` and `"proxy": "vercel"`:

```bash
curl -i -H 'Content-Type: application/json' \
  -d '{"image_url":"https://example.com/shoe.jpg","proxy":"vercel","max_matches":5}' \
  http://localhost:8000/v1/search
```

**Local channel only:** upload a file or omit `proxy`:

```bash
curl -F 'image=@shoe.jpg' -F 'lang=zh-CN' http://localhost:8000/v1/search
curl -H 'Content-Type: application/json' \
  -d '{"image_url":"https://example.com/shoe.jpg","max_matches":5}' \
  http://localhost:8000/v1/search
```

- Pass `"proxy": "vercel"` on every request that should use Vercel-first routing with local fallback (`GLENS_PROXY_FALLBACK=1`). Omitting `proxy` routes strictly to local slots.
- `proxy: "vercel"` requires `image_url` and does not support `resolve_links: true`.
- Uploads are capped at 10 MiB and normalized to compact JPEG (`GLENS_MAX_IMAGE_EDGE=1024`) before upload.

### 3. Endpoints, headers, and errors

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/healthz` | `GET` | Liveness probe (`{"ok": true}`) |
| `/v1/status` | `GET` | Session readiness (`default_backend`, `cache_enabled`, `req_ready`, `jar_age_seconds`) |
| `/v1/pool/status` | `GET` | Live route pool telemetry: per-slot health, cooldown reason/remaining seconds, rolling 60s RPM, counters, and L1/L2 cache stats |
| `/v1/warmup` | `POST` | Reset cooldowns and mint fresh session cookies on demand (optional JSON `{"slot_id": null, "reset_cooldown": true, "force": true}`) |
| `/v1/search` | `POST` | Visual product search returning `top_title`, `titles`, and `matches` |

Response headers:
- **`X-Request-Id`**: per-request trace ID (echoes a safe client-supplied `X-Request-Id` when provided), matching `req_id=` in server logs.
- **`X-Glens-Route`**: winning cache tier or route (`cache-l1`, `cache-l2`, `vercel-0`, `local-0`).

Errors use `{"error":{"code":"…","message":"…"}}`: `413` too large, `415` unsupported media/invalid image, `422` invalid input or non-public URL, `429 upstream_rate_limited` (with `Retry-After`), `502` (`image_download_failed`, `lens_lookup_failed`, or `proxy_*`), and `503 upstream_bot_challenge` (with `Retry-After`) when all routes are cooling down after Google `/sorry` challenges.

### 4. Routing, caching, and production logs

- **RoutePool & self-healing:** `GLENS_PROXY_FALLBACK=1` routes `proxy: "vercel"` requests to healthy Vercel slots first and automatically fails over / load-shares to local slots (`curl_cffi` HTTP/2 transport + rolling per-route cookies + single-flight Chromium refresh that re-syncs fresh cookies back to Vercel). Scale out by adding comma-separated `GLENS_VERCEL_PROXY_URLS` or `GLENS_LOCAL_PROXIES`.
- **Two-level cache:** Docker enables `GLENS_CACHE=1` (including proxy mode) with L1 SHA-256 exact hits, L2 64-bit dHash near-duplicate hits (`GLENS_CACHE_DHASH_THRESHOLD=4`), and in-flight request coalescing.
- **Rotating production logs:** The server writes UTC millisecond-timestamped structured logs to both stderr (`docker compose logs -f`) and `/data/logs/glens.log` (20 MiB × 5 backups by default; configurable via `GLENS_LOG_LEVEL`, `GLENS_LOG_FILE`, `GLENS_LOG_MAX_BYTES`, `GLENS_LOG_BACKUP_COUNT`). Query strings on image URLs and proxy secrets are never logged.

```bash
docker compose logs -f glens
docker compose exec glens tail -n 200 /data/logs/glens.log
docker compose exec glens grep 'req_id=<X-Request-Id>' /data/logs/glens.log
docker compose exec glens grep -E 'WARNING|ERROR|Chromium|cooling down' /data/logs/glens.log
```

### 5. Building and pushing versioned images to Docker Hub

Use `scripts/docker-release.sh` to build, tag (`<version>`, `sha-<commit>`, and `latest`), and push releases (defaults to `linux/amd64` for cloud VMs such as DigitalOcean):

```bash
docker login
# Optionally set DOCKER_IMAGE=youruser/chrome-lens-search in .env
./scripts/docker-release.sh v1.0.0 --image youruser/chrome-lens-search
./scripts/docker-release.sh v1.0.0 --platform linux/amd64,linux/arm64 --git-tag
```

Deploy a pinned release tag on a server without rebuilding:

```bash
GLENS_IMAGE=youruser/chrome-lens-search:v1.0.0 docker compose pull
GLENS_IMAGE=youruser/chrome-lens-search:v1.0.0 docker compose up -d --no-build
```

**Local Clash Fake-IP note:** If Docker DNS resolves image hosts to `198.18.x.x`, either exclude those domains in Clash `dns.fake-ip-filter` or set `GLENS_ALLOW_FAKE_IP=1` in your local `.env` (allows `198.18.0.0/15` only; loopback, RFC1918, and cloud metadata IPs remain blocked). Keep `GLENS_ALLOW_FAKE_IP=0` on public cloud servers.

To serve directly from a checkout, install `pip install ".[server,browser]"` and run `glens serve` (default `127.0.0.1:8000`; use `--host` and `--port` to override).

---

## Why this exists

Google Lens is uniquely good at naming the *exact* product in a photo. But:

- **SerpApi's Lens endpoint is paid.**
- The free Python Lens libraries (`chrome-lens-py` et al.) do **OCR/translation only** — they can't give you the product/shopping matches.
- Lens from a plain server just gets blocked.

This library talks to Google's **own** Lens upload endpoint (the image goes only to Google, never a third-party host) and reads the product matches straight back.

## Install

```bash
pip install "chrome-lens-search[browser]"   # recommended: works out of the box
```

The `[browser]` extra (Selenium + undetected-chromedriver) is only used to create — and occasionally refresh — the Google session. On a machine that already has a session jar (see `GLENS_COOKIE_FILE` below), the slim core is enough:

```bash
pip install chrome-lens-search              # plain-HTTP core only (needs an existing jar)
```

## CLI

```bash
glens sneaker.jpg                            # pretty-printed results
glens https://example.com/sneaker.jpg        # image URLs work too
glens sneaker.jpg --json                     # machine-readable output
glens sneaker.jpg --backend req -v           # fast path only, with progress logs
glens sneaker.jpg --max-matches 5 --max-titles 3
glens sneaker.jpg --lang de                  # prefer German result pages
glens sneaker.jpg --cache                    # reuse recent results for this image
```

(`glens search sneaker.jpg` also works, if you prefer the explicit subcommand.)

Pretty output shows the top title, the frequency-ranked titles, and the merchant links; `--json` emits the full `LensResult` (add `--raw` to include every harvested anchor). Exit code `0` means results were found, `1` means no results, so it scripts cleanly:

```bash
glens photo.jpg --json > result.json || echo "nothing matched"
```

## How it works — the fast/slow hybrid

Google only server-renders the product tiles for a **JS-verified session**. A cold `requests` client just gets an "enable JavaScript" shell. The trick:

1. **Browser (once):** drive a real [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected_chromedriver) session, upload the image, harvest the rendered results — and **save the google.com cookies + user-agent** to a small jar.
2. **Requests (every time after):** replay those cookies over plain `requests`. Google now serves the full product HTML to a browser-less client. Fast, no window, no Selenium in the hot path.
3. **Self-healing:** when the jar goes stale, Google serves the degraded JS-gate page; the `req` path detects it *by content* (too few product links, not by sniffing for a marker) and transparently falls back to the browser, which re-mints the jar. You never manage cookies by hand.

So: **first call launches Chrome; subsequent calls are just HTTP** until the cookies expire.

## Backends

| `backend` | Behaviour |
|-----------|-----------|
| `"auto"` (default) | Try `req`, fall back to `browser` if there's no/stale jar. |
| `"req"` | Plain HTTP only. Returns `None` if the jar is missing/stale (won't launch Chrome). |
| `"browser"` | Always drive Chrome; also re-mints the jar. |

```python
glens.search("img.jpg", backend="req")     # fast path only
glens.search("img.jpg", backend="browser") # force a fresh browser pass
```

### Bring your own Chrome

The browser backend defaults to the bundled builder, but you can inject any factory that returns a Selenium-compatible driver — handy if you already run a stealth/persistent Chrome:

```python
def my_driver():
    return make_my_undetected_chrome()

glens.search("img.jpg", driver_factory=my_driver)
```

## The result

`search()` returns a `LensResult` (or `None` on failure — it never raises for ordinary network/parse errors):

```python
@dataclass
class LensResult:
    top_title: str | None      # best single product name
    titles: list[str]          # frequency-ranked product names
    matches: list[Match]       # external merchant/result links
    raw_anchors: list[dict]    # every anchor harvested, unfiltered
    results_url: str | None    # internal, session-bound Google URL (logging only; not browser-openable)

@dataclass
class Match:
    title: str
    url: str
    domain: str
```

`titles` are frequency-ranked: the *exact* product repeats across tiles, so it rises to the top on its own.

For batches there's `search_many`, which runs the images in order through the same in-process lock (be gentle — see the caveats):

```python
results = glens.search_many(["a.jpg", "b.jpg"], backend="auto")  # list of LensResult | None
```

## Automate it

The API is built to be dropped into your own code: one call in, one dataclass (or `None`) out, nothing raises for ordinary failures. Two helpers cover the ops side:

```python
import glens

# At startup / deploy time: make sure the fast path is ready.
if not glens.status()["req_ready"]:
    glens.warmup()                      # one headless Chrome run (needs [browser])

# In your pipeline: pure HTTP from here on, ~2-3s per lookup.
for path in my_images:
    result = glens.search(path, backend="req")
    if result:
        print(path, "->", result.top_title)
```

- **`glens.status()`** — JSON-safe health dict: is the session jar valid, how old is it, is the browser extra installed, is the fast path (`req_ready`) available. Same thing on the CLI: `glens status --json` (exit 0 = ready), perfect for container health checks.
- **`glens.warmup()`** — mints the jar with one browser run and returns `True` when ready; a no-op if the jar already exists. CLI: `glens warmup`. Run it once at container start or CI setup, then use `backend="req"` at runtime so production code can never accidentally launch Chrome.
- **Headless servers without Chrome:** run `glens warmup` on any machine that has Chrome, ship the jar file (`GLENS_COOKIE_FILE`), and the server only ever needs the slim `requests` core.
- **Batch jobs:** see [examples/batch_folder.py](examples/batch_folder.py) — folder in, CSV of product matches out. `search_many()` processes items sequentially; independent calls can overlap, so keep concurrency modest and don't hammer Google.

For machine consumption, `result.to_dict()` gives you JSON-ready output, and the CLI's `--json` + exit codes (0 results / 1 none / 2 usage) script cleanly.

## Configuration

Everything is a per-call argument; env vars are just the defaults.

| Argument | Env var | Default |
|----------|---------|---------|
| `backend` | `GLENS_BACKEND` | `auto` |
| `cookie_file` | `GLENS_COOKIE_FILE` | `~/.cache/glens/cookies.json` |
| `max_matches` | `GLENS_MAX_MATCHES` | `20` |
| `max_titles` | `GLENS_MAX_TITLES` | `6` |
| `timeout` | `GLENS_RESULT_TIMEOUT` | `25` |
| `lang` | `GLENS_LANG` | *(none)* — preferred response language via `Accept-Language`, e.g. `de`, `pt-BR` |
| `use_cache` | `GLENS_CACHE` | off |
| — | `GLENS_CACHE_TTL` | `86400` — cache entry lifetime in seconds |
| — | `GLENS_CACHE_DIR` | `~/.cache/glens/results` (`/data/results` in Docker) |
| — | `GLENS_CACHE_DHASH_THRESHOLD` | `4` — L2 64-bit dHash Hamming distance threshold (`0` disables L2) |
| — | `GLENS_CACHE_MAX_ENTRIES` | `10000` — max cache files before oldest eviction |
| — | `GLENS_MAX_IMAGE_EDGE` | `1024` — longest edge normalization in pixels (`0` disables resize) |
| — | `GLENS_RESOLVE_MAX` | `5` — max links resolved per call when `resolve_links=True` |
| — | `GLENS_HEADLESS` | `1` (`0` in Docker with Xvfb) |
| — | `GLENS_CHROME_PROFILE` | *(none)* — persistent Chrome profile dir (`/data/chrome-profile` in Docker) |
| — | `GLENS_VERCEL_PROXY_URL` / `GLENS_VERCEL_PROXY_URLS` | *(none)* — single or comma-separated Vercel proxy HTTPS URLs |
| — | `GLENS_VERCEL_PROXY_SECRET` / `GLENS_VERCEL_PROXY_SECRETS` | *(none)* — shared secret(s) matching Vercel `CRON_SECRET` |
| — | `GLENS_PROXY_FALLBACK` | `1` — fail over / load-share from Vercel to local slots |
| — | `GLENS_VERCEL_RPM` | `50` (Compose) / `20` (default) — per-slot RPM cap (`0` = unlimited) |
| — | `GLENS_LOCAL_RPM` | `30` (Compose) / `15` (default) — per-slot RPM cap (`0` = unlimited) |
| — | `GLENS_SLOT_MIN_INTERVAL` | `0.3` (Compose) / `0.5` (default) — minimum spacing per slot (seconds) |
| — | `GLENS_PROXY` / `GLENS_LOCAL_PROXIES` | *(none)* — outbound proxy URL(s) for local HTTP & Chromium refreshes |
| — | `GLENS_ALLOW_FAKE_IP` | `0` — local-dev opt-in to allow Clash Fake-IP (`198.18.0.0/15`) |
| — | `GLENS_LOG_LEVEL` | `INFO` — server log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| — | `GLENS_LOG_FILE` | `/data/logs/glens.log` (Docker) — rotating log file path |
| — | `GLENS_LOG_MAX_BYTES` | `20971520` — max bytes per log file (20 MiB) |
| — | `GLENS_LOG_BACKUP_COUNT` | `5` — rotated backup log files to keep |

Pointing `GLENS_CHROME_PROFILE` at a persistent directory lets the verified session survive across runs, keeping the fast path fast.

### Language

`lang` asks Google for a response language via the fast path's `Accept-Language` header:

```python
glens.search("img.jpg", lang="de")   # prefer German result pages
```

Best-effort by design: Google decides, and the country/market always follows your IP. (There's deliberately no `hl`/`gl` URL override — the Lens results URL is session-signed and Google answers any query tampering, on either the upload or the results request, with a 403. Live-tested.)

### Result caching (opt-in)

`use_cache=True` (or `--cache`, or `GLENS_CACHE=1`) stores the harvested anchors per image+locale (SHA-256 keyed) under `~/.cache/glens/results` and reuses them for 24 hours (`GLENS_CACHE_TTL`). Great for dev loops and re-runs — it avoids re-uploading the same image to Google. `max_matches`/`max_titles` still apply per call; only the raw harvest is cached. An explicit `backend="browser"` always skips the cache *read* (a successful browser run still *refreshes* the cached entry), so forcing a jar re-mint keeps working with caching on.

## Troubleshooting

**"No results" on the very first run** → the first lookup needs Chrome to create the session. Install the extra and retry: `pip install "chrome-lens-search[browser]"`. The CLI prints this hint when it applies.

**Want a server with no Chrome at all?** Run one lookup on any machine that has Chrome, then copy the jar (`~/.cache/glens/cookies.json`) to the server and point `GLENS_COOKIE_FILE` at it. Use `backend="req"` so the server never tries to launch a browser. When the jar eventually goes stale, re-mint it the same way.

**Nothing prints / want to see progress?** Add `-v` on the CLI, or in Python: `logging.basicConfig(level=logging.INFO)` — all progress goes to the `glens` logger.

## Caveats — read these

- **This scrapes an undocumented Google endpoint.** It is not an official API, and it's against Google's Terms of Service. Use it where that's acceptable to you.
- **It will break.** Google changes the endpoint, the JS-gate behaviour, and the markup without notice. When that happens the harvesting is deliberately *selector-light* (anchors + aria-labels, no hardcoded class names) so it degrades rather than shatters — but expect to patch it. PRs welcome.
- **Results are noisy by design.** The parser favours recall; verify matches downstream if you need precision.
- **Be gentle.** Searches can run concurrently. Keep batches modest; the endpoint publishes no guaranteed rate limit, and aggressive use can trigger 403/429 responses or age the session faster.

## Documentation

- **[Guide](docs/GUIDE.md)** — the long-form walkthrough: the fast/slow hybrid, automating glens in your code, deployment recipes (Docker, Chrome-less servers, CI), the CLI in depth, troubleshooting, and how it works under the hood.
- **[API Reference](docs/API.md)** — every function, dataclass, environment variable, CLI flag, and exit code, with exact signatures and return/error semantics.
- **[Best Practices](docs/BEST_PRACTICES.md)** — what to actually do: backend choice, minting vs serving, being gentle, handling `None`, caching strategy, deployment, jar hygiene, security, and testing your integration.

## Development

```bash
pip install -e ".[dev]"
pytest          # hermetic: no network, no browser
ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) — especially the rules about synthetic test fixtures (never commit Google content).

## License

MIT — see [LICENSE](LICENSE).

> Not affiliated with, endorsed by, or sponsored by Google. "Google Lens" is a trademark of Google LLC.
