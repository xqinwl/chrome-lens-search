# qinwl/chrome-lens-search

> **Unofficial.** Not affiliated with, endorsed by, or sponsored by Google. "Google Lens" is a trademark of Google LLC. For research and educational use.

Ready-to-run **Google Lens visual product search HTTP API** container with bundled Chromium, `curl_cffi`, virtual display (`Xvfb`), anti-bot route pooling with automatic Vercel-to-local failover, two-level caching (SHA-256 + perceptual dHash), and rotating production logs. Benchmarked at **60+ unique image searches/min**.

- **Architecture**: `linux/amd64` (ready for DigitalOcean, AWS, and standard x86_64 Linux VMs)
- **Tags**:
  - `v1.0.0` (and other semver tags): pinned releases recommended for production
  - `sha-<commit>`: exact Git commit snapshots for deterministic rollbacks
  - `latest`: latest stable release

---

## 1. Quick Start (Docker Compose — Recommended)

Create `compose.yaml` and `.env` in any directory on your server — **no Git clone required**.

### `compose.yaml`

```yaml
services:
  glens:
    image: qinwl/chrome-lens-search:latest
    container_name: glens
    restart: unless-stopped
    init: true
    environment:
      # Vercel proxy (single URL or comma-separated multi-region URLs)
      GLENS_VERCEL_PROXY_URL: ${GLENS_VERCEL_PROXY_URL:-}
      GLENS_VERCEL_PROXY_URLS: ${GLENS_VERCEL_PROXY_URLS:-}
      GLENS_VERCEL_PROXY_SECRET: ${GLENS_VERCEL_PROXY_SECRET:-}
      GLENS_VERCEL_PROXY_SECRETS: ${GLENS_VERCEL_PROXY_SECRETS:-}
      # Routing, failover, and rate limits (tuned for 60+ req/min)
      GLENS_PROXY_FALLBACK: ${GLENS_PROXY_FALLBACK:-1}
      GLENS_VERCEL_RPM: ${GLENS_VERCEL_RPM:-50}
      GLENS_LOCAL_RPM: ${GLENS_LOCAL_RPM:-30}
      GLENS_SLOT_MIN_INTERVAL: ${GLENS_SLOT_MIN_INTERVAL:-0.3}
      # Optional outbound proxy for local channel & Chromium cookie refreshes
      GLENS_PROXY: ${GLENS_PROXY:-}
      GLENS_LOCAL_PROXIES: ${GLENS_LOCAL_PROXIES:-}
      # Caching & image normalization
      GLENS_CACHE: ${GLENS_CACHE:-1}
      GLENS_CACHE_DHASH_THRESHOLD: ${GLENS_CACHE_DHASH_THRESHOLD:-4}
      GLENS_MAX_IMAGE_EDGE: ${GLENS_MAX_IMAGE_EDGE:-1024}
      GLENS_RESOLVE_MAX: ${GLENS_RESOLVE_MAX:-5}
      # Rotating production logs (persisted in /data/logs/glens.log)
      GLENS_LOG_LEVEL: ${GLENS_LOG_LEVEL:-INFO}
      GLENS_LOG_FILE: ${GLENS_LOG_FILE:-/data/logs/glens.log}
      GLENS_LOG_MAX_BYTES: ${GLENS_LOG_MAX_BYTES:-20971520}
      GLENS_LOG_BACKUP_COUNT: ${GLENS_LOG_BACKUP_COUNT:-5}
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - glens-data:/data

volumes:
  glens-data:
```

### `.env`

```env
GLENS_VERCEL_PROXY_URL=https://your-vercel-app.vercel.app/api/lens
GLENS_VERCEL_PROXY_SECRET=your-secret
```

### Pull, Start, and Warm Up

```bash
docker compose pull
docker compose up -d

# Liveness check
curl http://127.0.0.1:8000/healthz

# Prewarm session (launches Chromium once to mint cookies and sync them to Vercel slots)
curl -X POST http://127.0.0.1:8000/v1/warmup

# Inspect route pool health and cache stats
curl http://127.0.0.1:8000/v1/pool/status
```

> **Sizing tip:** At least **2 GB RAM** (or 1 GB RAM + 2 GB swap) is recommended on cloud VMs so Chromium cookie refreshes never hit OOM.

---

## 2. One-Line `docker run`

```bash
docker run -d --name glens --init --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v glens-data:/data \
  -e GLENS_VERCEL_PROXY_URL="https://your-vercel-app.vercel.app/api/lens" \
  -e GLENS_VERCEL_PROXY_SECRET="your-secret" \
  -e GLENS_VERCEL_RPM=50 \
  -e GLENS_LOCAL_RPM=30 \
  -e GLENS_SLOT_MIN_INTERVAL=0.3 \
  qinwl/chrome-lens-search:latest
```

---

## 3. HTTP API Usage

### Option A: Vercel-First with Automatic Local Failover (Recommended)

Send JSON with a public `image_url` and `"proxy": "vercel"`:

```bash
curl -i -X POST http://127.0.0.1:8000/v1/search \
  -H 'Content-Type: application/json' \
  -d '{
    "image_url": "https://example.com/shoe.jpg",
    "proxy": "vercel",
    "max_matches": 5,
    "lang": "en"
  }'
```

### Option B: Local Channel File Upload

```bash
curl -i -X POST http://127.0.0.1:8000/v1/search \
  -F 'image=@shoe.jpg' \
  -F 'max_matches=5' \
  -F 'lang=en'
```

> **Notes:**
> - Include `"proxy": "vercel"` on requests that should use **Vercel-first routing with automatic failover / load-sharing to local slots** (`GLENS_PROXY_FALLBACK=1`). Omitting `proxy` routes strictly to local slots.
> - `proxy: "vercel"` requires `image_url` and does not support `resolve_links: true`.
> - Maximum image size is 10 MiB; oversized images are automatically resized (`GLENS_MAX_IMAGE_EDGE=1024`) to compact JPEG before upload.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | `GET` | Liveness probe (`{"ok": true}`) |
| `/v1/status` | `GET` | Basic session status (`default_backend`, `cache_enabled`, `req_ready`, `jar_age_seconds`) |
| `/v1/pool/status` | `GET` | Live route pool telemetry: slot health, cooldown reason/remaining seconds, rolling 60s RPM, counters, and L1/L2 cache stats |
| `/v1/warmup` | `POST` | Reset cooldowns and mint fresh session cookies on demand (optional JSON: `{"slot_id": null, "reset_cooldown": true, "force": true}`) |
| `/v1/search` | `POST` | Visual product search returning `top_title`, `titles`, and `matches` |

### Response Headers & Error Codes

- **`X-Request-Id`**: per-request trace ID (echoes a safe client-provided `X-Request-Id`), matching `req_id=` in server logs.
- **`X-Glens-Route`**: winning cache tier or upstream route (`cache-l1`, `cache-l2`, `vercel-0`, `local-0`).
- **`422 invalid_image_url`**: image URL or redirect did not resolve to a public IP (SSRF protection).
- **`429 upstream_rate_limited`**: Google Lens rate limit or all route slots reached RPM caps (includes `Retry-After`).
- **`503 upstream_bot_challenge`**: all available routes hit Google `/sorry` challenges and are cooling down (includes `Retry-After`).

---

## 4. Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GLENS_VERCEL_PROXY_URL` / `GLENS_VERCEL_PROXY_URLS` | *(empty)* | Single or comma-separated Vercel proxy HTTPS URLs |
| `GLENS_VERCEL_PROXY_SECRET` / `GLENS_VERCEL_PROXY_SECRETS` | *(empty)* | Shared secret(s) matching Vercel `CRON_SECRET` |
| `GLENS_PROXY_FALLBACK` | `1` | Automatically fail over / load-share from Vercel to local slots |
| `GLENS_VERCEL_RPM` | `50` (Compose) / `20` (image) | Per-slot RPM limit for Vercel slots (`0` = unlimited) |
| `GLENS_LOCAL_RPM` | `30` (Compose) / `15` (image) | Per-slot RPM limit for local slots (`0` = unlimited) |
| `GLENS_SLOT_MIN_INTERVAL` | `0.3` (Compose) / `0.5` (image) | Minimum spacing between requests per slot (seconds) |
| `GLENS_PROXY` / `GLENS_LOCAL_PROXIES` | *(empty)* | Outbound proxy URL(s) for local HTTP & Chromium refreshes (helpful if datacenter IP hits `/sorry`) |
| `GLENS_CACHE` | `1` | Enable two-level local cache (L1 SHA-256 + L2 dHash near-duplicate) |
| `GLENS_CACHE_DHASH_THRESHOLD` | `4` | L2 64-bit dHash Hamming distance threshold (`0` disables L2) |
| `GLENS_MAX_IMAGE_EDGE` | `1024` | Max image edge in pixels before JPEG normalization (`0` disables) |
| `GLENS_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `GLENS_LOG_FILE` | `/data/logs/glens.log` | Persistent rotating log file (20 MiB × 5 backups by default) |

---

## 5. Production Logs & Troubleshooting

Structured UTC millisecond-timestamped logs are written to both stderr and the persistent `/data/logs/glens.log` volume (URL query strings and secrets are stripped automatically):

```bash
# Live logs
docker compose logs -f glens

# Inspect persistent log file (survives container recreates)
docker compose exec glens tail -n 200 /data/logs/glens.log

# Trace a single request using X-Request-Id
docker compose exec glens grep 'req_id=<X-Request-Id>' /data/logs/glens.log

# Filter warnings, errors, cooldowns, and Chromium cookie refreshes
docker compose exec glens grep -E 'WARNING|ERROR|Chromium|cooling down' /data/logs/glens.log
```

## 6. Upgrading

```bash
# Update the tag in compose.yaml (or track :latest), then:
docker compose pull && docker compose up -d
```
