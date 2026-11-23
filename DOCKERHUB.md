# qinwl/chrome-lens-search

> **非官方项目声明**：本项目基于 `chrome-lens-search`，非 Google 官方镜像，仅供学习与研究使用。

开箱即用的 **Google Lens 以图搜商品 HTTP 服务镜像**（内置 Chromium、`curl_cffi`、虚拟显示器 `Xvfb`、路由池控频熔断、两级缓存与轮转日志）。支持 **Vercel 代理优先 + 本地通道自动分流/容灾**，实测可稳定达到 **60+ 次/分钟**。

- **支持架构**：`linux/amd64`（直接适配 DigitalOcean / AWS / 各类 Linux x86_64 云服务器）
- **版本 Tag 说明**：
  - `v1.0.0` 等语义化版本号：生产环境推荐使用固定版本
  - `sha-<commit>`：精确对应 Git 提交，便于回滚
  - `latest`：最新稳定版

---

## 1. 快速启动（推荐 Docker Compose）

在服务器任意目录创建 `compose.yaml` 和 `.env`，**无需克隆源码仓库**。

### `compose.yaml`

```yaml
services:
  glens:
    image: qinwl/chrome-lens-search:latest
    container_name: glens
    restart: unless-stopped
    init: true
    environment:
      # Vercel 代理（支持单个或多个逗号分隔 URL）
      GLENS_VERCEL_PROXY_URL: ${GLENS_VERCEL_PROXY_URL:-}
      GLENS_VERCEL_PROXY_URLS: ${GLENS_VERCEL_PROXY_URLS:-}
      GLENS_VERCEL_PROXY_SECRET: ${GLENS_VERCEL_PROXY_SECRET:-}
      GLENS_VERCEL_PROXY_SECRETS: ${GLENS_VERCEL_PROXY_SECRETS:-}
      # 路由分流与控频（默认目标 60+ req/min）
      GLENS_PROXY_FALLBACK: ${GLENS_PROXY_FALLBACK:-1}
      GLENS_VERCEL_RPM: ${GLENS_VERCEL_RPM:-50}
      GLENS_LOCAL_RPM: ${GLENS_LOCAL_RPM:-30}
      GLENS_SLOT_MIN_INTERVAL: ${GLENS_SLOT_MIN_INTERVAL:-0.3}
      # 可选：本地通道与 Chromium 续期的出口代理（机房 IP 遇验证码时使用）
      GLENS_PROXY: ${GLENS_PROXY:-}
      GLENS_LOCAL_PROXIES: ${GLENS_LOCAL_PROXIES:-}
      # 缓存与图片归一化
      GLENS_CACHE: ${GLENS_CACHE:-1}
      GLENS_CACHE_DHASH_THRESHOLD: ${GLENS_CACHE_DHASH_THRESHOLD:-4}
      GLENS_MAX_IMAGE_EDGE: ${GLENS_MAX_IMAGE_EDGE:-1024}
      GLENS_RESOLVE_MAX: ${GLENS_RESOLVE_MAX:-5}
      # 生产日志（持久化在 /data/logs/glens.log，自动轮转）
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

### 启动与预热

```bash
docker compose pull
docker compose up -d

# 检查健康状态
curl http://127.0.0.1:8000/healthz

# 预热会话（启动 Chromium 签发 Cookie 并同步到 Vercel 槽位）
curl -X POST http://127.0.0.1:8000/v1/warmup

# 查看路由池状态
curl http://127.0.0.1:8000/v1/pool/status
```

> **云服务器规格建议**：建议至少 **2 GB 内存**（或 1 GB 内存 + 2 GB Swap），确保 Chromium 自动续期不会 OOM。

---

## 2. 单行 `docker run` 启动

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

## 3. HTTP API 调用方式

### 方式 A：Vercel 优先 + 本地自动分流/容灾（推荐）

使用 JSON 提交公网 `image_url`，并带上 `"proxy": "vercel"`：

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

### 方式 B：本地通道上传图片文件

```bash
curl -i -X POST http://127.0.0.1:8000/v1/search \
  -F 'image=@shoe.jpg' \
  -F 'max_matches=5' \
  -F 'lang=zh-CN'
```

> **说明**：
> - 请求体带上 `"proxy": "vercel"` 才会走 **Vercel 优先 + 本地容灾/分流**；不带 `proxy` 时仅走本地通道。
> - `proxy: "vercel"` 仅支持 `image_url`，不支持 `resolve_links: true`。
> - 单图大小上限 10 MiB，默认按长边 `1024px` 自动归一化压缩上传。

### 全部接口列表

| 接口 | 方法 | 说明 |
|------|------|------|
| `/healthz` | `GET` | 存活检查，返回 `{"ok": true}` |
| `/v1/status` | `GET` | 会话基础状态（`req_ready`、`jar_age_seconds` 等） |
| `/v1/pool/status` | `GET` | 路由池实时状态、冷却原因与剩余时间、近 1 分钟 RPM、L1/L2 缓存统计 |
| `/v1/warmup` | `POST` | 重置冷却并刷新会话 Cookie（可选 JSON：`{"slot_id": null, "reset_cooldown": true, "force": true}`） |
| `/v1/search` | `POST` | 执行以图搜商品，返回 `top_title`、`titles`、`matches` |

### 响应头与常见状态码

- **`X-Request-Id`**：单次请求追踪 ID（支持客户端自定义透传），与日志中 `req_id=` 对应。
- **`X-Glens-Route`**：实际命中的路由或缓存层（`cache-l1`、`cache-l2`、`vercel-0`、`local-0`）。
- **`422 invalid_image_url`**：图片 URL 或重定向未解析为公网 IP（SSRF 防护）。
- **`429 upstream_rate_limited`**：触发限流或所有槽位到达 RPM 上限（附带 `Retry-After`）。
- **`503 upstream_bot_challenge`**：所有出口触发 Google `/sorry` 验证码并进入冷却（附带 `Retry-After`）。

---

## 4. 关键环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `GLENS_VERCEL_PROXY_URL` / `GLENS_VERCEL_PROXY_URLS` | *(空)* | 单个或多个逗号分隔的 Vercel 代理 HTTPS 地址 |
| `GLENS_VERCEL_PROXY_SECRET` / `GLENS_VERCEL_PROXY_SECRETS` | *(空)* | Vercel 代理密钥（与 Vercel 侧 `CRON_SECRET` 一致） |
| `GLENS_PROXY_FALLBACK` | `1` | Vercel 限流/异常/冷却时自动降级分流到本地通道 |
| `GLENS_VERCEL_RPM` | `50` (Compose) / `20` (镜像默认) | 单个 Vercel 槽位每分钟请求上限（`0` 不限） |
| `GLENS_LOCAL_RPM` | `30` (Compose) / `15` (镜像默认) | 单个本地槽位每分钟请求上限（`0` 不限） |
| `GLENS_SLOT_MIN_INTERVAL` | `0.3` (Compose) / `0.5` (镜像默认) | 单槽位请求最小间隔（秒） |
| `GLENS_PROXY` / `GLENS_LOCAL_PROXIES` | *(空)* | 本地通道与 Chromium 续期出口代理（云机房 IP 遇验证码时配置） |
| `GLENS_CACHE` | `1` | 开启两级本地缓存（L1 SHA-256 + L2 dHash 近似去重） |
| `GLENS_CACHE_DHASH_THRESHOLD` | `4` | L2 近似图感知哈希汉明距离阈值（`0` 关闭 L2） |
| `GLENS_MAX_IMAGE_EDGE` | `1024` | 上传图片最大长边像素（`0` 关闭缩放） |
| `GLENS_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `GLENS_LOG_FILE` | `/data/logs/glens.log` | 持久化轮转日志路径（默认 20 MiB × 5 份） |

---

## 5. 生产日志与排错复盘

日志同时输出到标准输出与持久卷 `/data/logs/glens.log`（容器重建后日志保留，自动脱敏 URL 查询参数与密钥）：

```bash
# 实时日志
docker compose logs -f glens

# 查看持久日志
docker compose exec glens tail -n 200 /data/logs/glens.log

# 按响应头里的 X-Request-Id 追踪单次请求
docker compose exec glens grep 'req_id=你的ID' /data/logs/glens.log

# 只看告警、报错、熔断冷却与 Chromium 续期
docker compose exec glens grep -E 'WARNING|ERROR|Chromium|cooling down' /data/logs/glens.log
```

## 6. 升级到新版本

```bash
# 修改 compose.yaml 中的 tag（或使用 latest）后执行：
docker compose pull && docker compose up -d
```
