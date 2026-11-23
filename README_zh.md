# chrome-lens-search

[![CI](https://github.com/darcodev/chrome-lens-search/actions/workflows/ci.yml/badge.svg)](https://github.com/darcodev/chrome-lens-search/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chrome-lens-search)](https://pypi.org/project/chrome-lens-search/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) | [简体中文](README_zh.md)

> **非官方项目声明**：本项目非 Google 官方项目，与 Google 没有任何关联，亦未获得 Google 的认可或赞助。“Google Lens” 是 Google LLC 的注册商标。本项目为社区独立开发，仅供研究与学习使用。

**基于普通 HTTP 的非官方 Google Lens 以图搜商品库。** 输入一张图片，获取 Google Lens 识别出的商品信息 —— 包括精确的商品标题和第三方商家/电商链接。无需支付昂贵的 SerpApi 账单，也没有常见开源 Lens 库仅支持 OCR/文字提取的限制。

---

## 快速上手

### 命令行使用

```bash
pip install "chrome-lens-search[browser]"
glens sneaker.jpg
```

就这么简单！**首次**运行会启动一次 Chrome 来建立可复用的 Google 会话（默认无头模式，若 Google 拦截无头模式可能会短暂弹出窗口）。**此后的每次查询都是极速的普通 HTTP 请求**（约 2 秒），热路径下不需要浏览器，也不需要 Selenium。

### Python 代码调用

支持本地文件路径、网络图片 URL 或二进制原始字节（Bytes）：

```python
import glens

result = glens.search("sneaker.jpg")                       # 本地文件
result = glens.search("https://example.com/sneaker.jpg")   # 或网络图片 URL

if result:                             # 查询失败或无匹配时返回 None
    print(result.top_title)            # "Nike Air Max 90 Men's Shoes"
    for m in result.matches:
        print(f"[{m.domain}] {m.title} -> {m.url}")
```

输出示例：

```text
[nike.com]      Nike Air Max 90 Men's Shoes -> https://www.nike.com/t/...
[stockx.com]    Nike Air Max 90 -> https://stockx.com/...
[goat.com]      Air Max 90 -> https://www.goat.com/...
```

## HTTP 服务与 Docker 部署

### 1. 配置 `.env` 并启动

在项目根目录创建 `.env`（已被 `.gitignore` 忽略，不会提交到仓库）：

```env
# 推荐：配置 Vercel 代理（支持单个 URL 或逗号分隔的多个区域 URL）
GLENS_VERCEL_PROXY_URL=https://your-vercel-app.vercel.app/api/lens
GLENS_VERCEL_PROXY_SECRET=your-secret

# 路由分流与控频（compose.yaml 默认即以下值，目标 60+ req/min）
GLENS_PROXY_FALLBACK=1
GLENS_VERCEL_RPM=50
GLENS_LOCAL_RPM=30
GLENS_SLOT_MIN_INTERVAL=0.3

# 可选：若云服务器机房 IP 频繁遇到 Google 验证码，可为本地通道配置出口代理
# GLENS_PROXY=http://user:pass@proxy-host:port
# GLENS_LOCAL_PROXIES=http://proxy1:8080,http://proxy2:8080

# 可选：仅本机使用 Clash Fake-IP (198.18.0.0/15) 调试时开启，生产环境请保持默认 0
# GLENS_ALLOW_FAKE_IP=1
```

启动服务并预热会话：

```bash
docker compose up -d --build
curl http://localhost:8000/healthz
curl -X POST http://localhost:8000/v1/warmup
curl http://localhost:8000/v1/pool/status
```

Compose 根据宿主机架构构建 Linux AMD64 或 ARM64 镜像，内置 Chromium、配套驱动、`curl_cffi` 和虚拟显示器（`Xvfb`）。宿主机通过本地 `8000` 端口访问（可用 `GLENS_PORT=8080` 修改），同一 Compose 网络内的其他容器可访问 `http://glens:8000`。命名卷 `glens-data` 挂载于 `/data`，持久保存 Cookie、Chrome Profile、结果缓存与轮转日志，重启或重建容器后均可复用。建议云服务器至少配置 **2 GB 内存**（或 1 GB 内存 + 2 GB Swap），以保证 Chromium 续期不发生 OOM。

### 2. 调用搜索接口

**推荐（启用 Vercel 优先 + 本地自动分流/容灾）：** 使用 JSON 提交公网 `image_url`，并带上 `"proxy": "vercel"`：

```bash
curl -i -H 'Content-Type: application/json' \
  -d '{"image_url":"https://example.com/shoe.jpg","proxy":"vercel","max_matches":5}' \
  http://localhost:8000/v1/search
```

**仅走本地容器通道：** 上传本地图片文件，或不传 `proxy` 字段：

```bash
curl -F 'image=@shoe.jpg' -F 'lang=zh-CN' http://localhost:8000/v1/search
curl -H 'Content-Type: application/json' \
  -d '{"image_url":"https://example.com/shoe.jpg","max_matches":5}' \
  http://localhost:8000/v1/search
```

> **注意**：
> - 客户端请求时**必须带上 `"proxy": "vercel"`** 才会进入 Vercel 优先 + 本地分流/容灾调度；不传 `proxy` 只会走本地 `local-*` 通道。
> - `proxy: "vercel"` 仅支持 `image_url` 输入，且不支持 `resolve_links: true`。
> - 两种输入均支持 `lang`、`max_matches`、`max_titles`、`resolve_links`（本地通道）；单图上限 10 MiB，服务端默认按长边 `GLENS_MAX_IMAGE_EDGE=1024` 归一化压缩为紧凑 JPEG 后上传。

### 3. HTTP 接口一览

| 接口 | 方法 | 说明 |
|------|------|------|
| `/healthz` | `GET` | 存活探针，返回 `{"ok": true}` |
| `/v1/status` | `GET` | 会话基础状态（`default_backend`、`cache_enabled`、`req_ready`、`jar_age_seconds`） |
| `/v1/pool/status` | `GET` | 路由池实时状态：各槽位健康度、冷却原因与剩余秒数、近 1 分钟 RPM、成功/验证码/限流/错误计数及 L1/L2 缓存统计 |
| `/v1/warmup` | `POST` | 在线重置冷却并刷新会话（可选 JSON：`{"slot_id": null, "reset_cooldown": true, "force": true}`） |
| `/v1/search` | `POST` | 执行以图搜商品，返回 `top_title`、`titles`、`matches`（确认无匹配时返回 HTTP 200 及空列表） |

### 4. 响应头与错误码

每次请求均返回以下观测响应头：
- **`X-Request-Id`**：单次请求追踪 ID（支持客户端在请求头传入自定义 `X-Request-Id` 透传），与服务端日志中的 `req_id=` 一一对应。
- **`X-Glens-Route`**：本次命中的具体路由或缓存层，如 `cache-l1`（SHA-256 精确命中）、`cache-l2`（dHash 近似图命中）、`vercel-0`、`local-0`。

错误响应格式统一为 `{"error":{"code":"…","message":"…"}}`：

| HTTP 状态码 | 错误码 (`code`) | 含义与处理建议 |
|-------------|-----------------|----------------|
| `413` | `request_too_large` / `image_too_large` | 请求体或图片超过 10 MiB 上限 |
| `415` | `unsupported_media_type` / `invalid_image` | Content-Type 非 JSON/multipart，或图片内容损坏 |
| `422` | `invalid_request` / `invalid_image_url` | 参数非法，或图片 URL / 重定向解析为非公网 IP（本机 Clash Fake-IP 见下文） |
| `429` | `upstream_rate_limited` | Google 返回 429 或所有可用槽位到达限速上限（附带 `Retry-After` 头，建议按该秒数退避） |
| `502` | `image_download_failed` / `lens_lookup_failed` / `proxy_*` | 图片源站无法下载，或 Lens 未返回可用结果页 |
| `503` | `upstream_bot_challenge` | 所有可用出口均触发 Google `/sorry` 机器人检测并进入熔断冷却（附带 `Retry-After` 头） |

### 5. 路由调度、缓存与扩容机制

- **Vercel 优先 + 自动分流/容灾**：开启 `GLENS_PROXY_FALLBACK=1`（默认）时，`RoutePool` 优先使用健康的 `vercel-*` 槽位；当 Vercel 触发 `/sorry`、429、代理异常、返回退化页或达到单路 RPM 上限时，自动冷却该槽位并无缝分流/降级到本地 `local-*` 通道。
- **自动 Cookie 滚动与单飞续期**：本地通道优先使用 `curl_cffi`（模拟 Chrome TLS/HTTP2 指纹）并发请求，并按路由隔离滚动回写 Cookie；遇到 JS-gate 时单飞唤起一次 Chromium 重新签发 Cookie 并自动同步给所有 Vercel 槽位（其余并发请求自动等待复用新会话，不会引发浏览器雪崩）。
- **两级本地缓存与并发合并**：Docker 默认开启 `GLENS_CACHE=1`（含 Vercel 代理模式），包含 L1 SHA-256 精确缓存、L2 感知哈希 dHash 近似图去重（`GLENS_CACHE_DHASH_THRESHOLD=4`）以及同图并发 Single-Flight 合并，减少重复上传。
- **横向扩容**：若长期持续高吞吐（如连续数小时每分钟 60+ 次），可在 `.env` 中通过 `GLENS_VERCEL_PROXY_URLS`（逗号分隔多个不同区域的 Vercel 实例）和/或 `GLENS_LOCAL_PROXIES`（逗号分隔多个本地出口代理）线性扩容，无需修改代码。

### 6. 生产日志与排错复盘

服务同时向 **stderr**（`docker compose logs -f`）和持久化卷文件 **`/data/logs/glens.log`** 写入带 UTC 毫秒时间戳的结构化日志（默认单文件 20 MiB、保留 5 份自动轮转，重建容器不丢日志）。日志自动剥离图片 URL 中的查询参数/签名，且不输出代理密钥或 Cookie 值。

单次请求日志示例：
```text
2026-11-23T04:19:13.007Z INFO [glens] search req_id=verify-log-001 status=200 route=vercel-0 proxy=vercel input=https://picsum.photos raw_bytes=48177 upload_bytes=48177 matches=2 titles=6 duration_ms=5854
```

生产复盘常用命令：

```bash
# 实时查看日志
docker compose logs -f glens

# 查看持久化日志文件（容器重建后仍保留）
docker compose exec glens tail -n 200 /data/logs/glens.log

# 用客户端拿到的 X-Request-Id 精确追踪单次请求
docker compose exec glens grep 'req_id=你的请求ID' /data/logs/glens.log

# 快速筛选异常、熔断冷却、限流与 Chromium 续期记录
docker compose exec glens grep -E 'WARNING|ERROR|Chromium|cooling down' /data/logs/glens.log
```

### 7. 打包发布镜像到 Docker Hub（按版本打 Tag）

项目提供了发布脚本 `scripts/docker-release.sh`，每次发新版时自动按版本 Tag + Git SHA Tag + `latest` 跨平台打包并推送（默认构建 `linux/amd64`，适配 DigitalOcean）：

```bash
# 1) 首次登录 Docker Hub，并在 .env 中配置目标仓库名（或通过 --image 传入）
docker login
# 在 .env 中添加：DOCKER_IMAGE=你的用户名/chrome-lens-search

# 2) 发布新版本（自动推送 :v1.0.0、:sha-<commit>、:latest）
./scripts/docker-release.sh v1.0.0

# 可选：同时构建 amd64 + arm64 双架构，并同步打 Git Tag
./scripts/docker-release.sh v1.0.0 --platform linux/amd64,linux/arm64 --git-tag

# 可选：测试版不覆盖 latest，或先 --dry-run 预览命令
./scripts/docker-release.sh v1.0.1-rc1 --no-latest
./scripts/docker-release.sh v1.0.0 --dry-run
```

在 DigitalOcean 上直接拉取指定版本镜像运行（无需在服务器上重新构建）：

```bash
GLENS_IMAGE=你的用户名/chrome-lens-search:v1.0.0 docker compose pull
GLENS_IMAGE=你的用户名/chrome-lens-search:v1.0.0 docker compose up -d --no-build
```

### 8. 常见部署问题

- **本机使用 Clash 时 `image_url` 返回 `422 invalid_image_url`**：Clash Fake-IP 会将公网域名解析为 `198.18.x.x`，被服务端 SSRF 安全校验拦截。**本机调试**可在 `.env` 中加 `GLENS_ALLOW_FAKE_IP=1` 后重启容器（仅放行 `198.18.0.0/15`，内网与云元数据 IP 仍严格拦截），或在 Clash `dns.fake-ip-filter` 中排除图片域名。**DigitalOcean 等公网云服务器无此问题，生产环境请保持 `GLENS_ALLOW_FAKE_IP=0`。**
- **云服务器机房 IP 遇到 `/sorry`**：若 `POST /v1/warmup` 或 `/v1/pool/status` 显示 `local-0` 触发 `bot_challenge`，在 `.env` 中配置 `GLENS_PROXY`（或 `GLENS_LOCAL_PROXIES`）为干净代理出口，Chromium 续期与本地通道会自动走该代理。
- **源码直接运行**：`pip install ".[server,browser]"` 后运行 `glens serve`（默认监听 `127.0.0.1:8000`，可用 `--host` 和 `--port` 调整）。

---

## 为什么需要这个项目？

Google Lens 在识别照片中**具体是哪一款商品**方面表现极其出色。然而：

- **SerpApi 的 Lens 接口价格昂贵**。
- 现有的免费 Python Lens 库（如 `chrome-lens-py` 等）**仅支持 OCR / 文本翻译** —— 无法获取商品和购物匹配项。
- 直接从普通服务器向 Lens 发起请求会被 Google 严格拦截（JS-Gate）。

本项目直接与 Google **原生**的 Lens 上传端点交互（图片仅发送给 Google，绝不经过任何第三方中转），并直接解析商品匹配结果。

---

## 安装

```bash
pip install "chrome-lens-search[browser]"   # 推荐：开箱即用
```

`[browser]` 额外依赖（Selenium + undetected-chromedriver）仅用于初次创建及偶尔刷新 Google 会话。在已经拥有有效 Cookie 会话文件的机器上（参考下文 `GLENS_COOKIE_FILE`），仅需安装轻量核心包即可：

```bash
pip install chrome-lens-search              # 仅包含纯 HTTP 核心（需已有 Cookie 会话文件）
```

---

## CLI 命令行工具

```bash
glens sneaker.jpg                            # 友好格式化输出
glens https://example.com/sneaker.jpg        # 支持网络图片 URL
glens sneaker.jpg --json                     # 输出机器可读的 JSON
glens sneaker.jpg --backend req -v           # 仅走 HTTP 快速通道，显示详细进度日志
glens sneaker.jpg --max-matches 5 --max-titles 3
glens sneaker.jpg --lang zh-CN               # 设置偏好语言为中文
glens sneaker.jpg --cache                    # 针对相同图片复用本地缓存
```

*(也支持 `glens search sneaker.jpg` 显式子命令风格。)*

默认输出会显示最佳标题、频次排序的候选标题以及商家链接；使用 `--json` 会输出完整的 `LensResult` 结构（加上 `--raw` 可包含解析到的所有未过滤原始锚点）。退出码说明：`0` 表示找到结果，`1` 表示无匹配结果或查询失败，方便编写自动化脚本：

```bash
glens photo.jpg --json > result.json || echo "未找到匹配结果"
```

---

## 工作原理 —— 快慢混合自愈架构

Google 仅向**通过 JavaScript 验证的会话**服务端渲染商品卡片。如果是全新且没有会话凭证的 `requests` 客户端，只会得到一个“请启用 JavaScript”的外壳页面。本项目的解决策略是：

1. **浏览器通道（仅运行一次）：** 驱动真实的 [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected_chromedriver) 会话，上传图片并抓取渲染后的结果 —— 同时将 **google.com 的 Cookie 及 User-Agent** 保存到本地 Cookie Jar。
2. **HTTP 通道（后续所有请求）：** 使用普通的 `requests` 携带这些 Cookie 重放请求。此时 Google 会直接向无浏览器的轻量客户端返回完整的商品 HTML。耗时仅需约 2 秒，无需弹窗，热路径下完全无 Selenium 开销。
3. **自动自愈（Self-healing）：** 当本地 Cookie 失效时，Google 会再次返回 JS 拦截页；`req` 后端会通过**内容特征**（检测到商品链接过少，而非脆弱的字符串标记）自动识别该状态，并无缝降级到浏览器通道重新生成 Cookie Jar。你完全不需要手动维护 Cookie。

总结：**首次运行或 Cookie 失效时调用 Chrome；平时所有查询均为纯 HTTP 请求。**

---

## 后端模式 (Backends)

| `backend` | 行为表现 |
|-----------|---------|
| `"auto"` (默认) | 优先尝试 `req`，若缺少 Cookie 或 Cookie 失效则自动降级到 `browser`。 |
| `"req"` | 仅走纯 HTTP 快速通道。如果 Cookie 缺失或失效则直接返回 `None`（绝不启动 Chrome）。 |
| `"browser"` | 强制启动 Chrome 浏览器执行查询，同时更新本地 Cookie Jar。 |

```python
glens.search("img.jpg", backend="req")     # 仅走快速 HTTP 通道
glens.search("img.jpg", backend="browser") # 强制使用浏览器并刷新会话
```

### 自定义 Chrome 驱动 (Bring your own Chrome)

浏览器模式默认使用内置构建器，但你也可以传入任何返回 Selenium 兼容 WebDriver 的自定义工厂函数（如果你已有现成的隐身/常驻 Chrome）：

```python
def my_driver():
    return make_my_undetected_chrome()

glens.search("img.jpg", driver_factory=my_driver)
```

---

## 返回结果说明

`search()` 返回一个 `LensResult` 对象（若查询失败返回 `None` —— 对于普通网络/解析错误从不抛出异常）：

```python
@dataclass
class LensResult:
    top_title: str | None      # 最佳单品名称
    titles: list[str]          # 频次排序的候选商品名称列表
    matches: list[Match]       # 外部商家 / 搜索结果链接
    raw_anchors: list[dict]    # 页面提取到的所有原始标签（未过滤）
    results_url: str | None    # 内部会话绑定的 Google 结果 URL（仅供日志排查，不可在普通浏览器打开）

@dataclass
class Match:
    title: str
    url: str
    domain: str
```

- **`titles` 频次排序**：因为同一款商品通常会在多个商家卡片中反复出现，所以出现频次最高的标题会自动排在最前（即 `top_title`）。
- **批量查询**：提供 `search_many` 方法，通过进程级锁依序处理多张图片：

```python
results = glens.search_many(["a.jpg", "b.jpg"], backend="auto")  # 返回 list[LensResult | None]
```

---

## 自动化与生产集成

该库设计为可直接集成进生产系统：输入参数简单，输出数据类，普通失败不抛异常。另外提供了两个运维辅助函数：

```python
import glens

# 在服务启动或部署时：确保快速通道已就绪
if not glens.status()["req_ready"]:
    glens.warmup()                      # 运行一次无头 Chrome 预热（需安装 [browser] 依赖）

# 在业务流水线中：纯 HTTP 调用，每次约 2~3 秒
for path in my_images:
    result = glens.search(path, backend="req")
    if result:
        print(path, "->", result.top_title)
```

- **`glens.status()`** —— 返回 JSON 友好的健康状态字典：Cookie 是否有效、有效期多长、是否安装了浏览器依赖、快速通道 (`req_ready`) 是否可用。CLI 对应命令：`glens status --json`（就绪时退出码为 0），非常适合容器健康检查。
- **`glens.warmup()`** —— 通过一次浏览器调用生成 Cookie Jar，就绪后返回 `True`；若 Cookie 已存在则直接跳过。CLI 对应命令：`glens warmup`。可以在容器启动或 CI 流程中执行一次，随后在业务代码中使用 `backend="req"`，杜绝生产环境意外唤起 Chrome。
- **无 Chrome 的无头服务器部署**：在任意装有 Chrome 的机器上运行 `glens warmup`，将生成的 Cookie 文件（`GLENS_COOKIE_FILE`）复制到无头服务器上，服务器只需安装不带 browser 依赖的纯 HTTP 核心包即可。
- **批处理任务**：参考 [examples/batch_folder.py](examples/batch_folder.py) —— 输入文件夹，输出商品匹配 CSV。单机内请求会被进程锁串行化，请合理控制调用频率，切勿高并发压测。

---

## 配置选项

所有配置均可作为函数调用参数传入；环境变量用作全局默认值。

| 参数名 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `backend` | `GLENS_BACKEND` | `auto` | 后端模式 (`auto`, `req`, `browser`) |
| `cookie_file` | `GLENS_COOKIE_FILE` | `~/.cache/glens/cookies.json` | 浏览器生成的 Cookie 文件路径 |
| `max_matches` | `GLENS_MAX_MATCHES` | `20` | 返回的外部商家链接最大数量 |
| `max_titles` | `GLENS_MAX_TITLES` | `6` | 返回的商品候选标题最大数量 |
| `timeout` | `GLENS_RESULT_TIMEOUT` | `25` | 结果页面加载超时时间（秒） |
| `lang` | `GLENS_LANG` | *(空)* | 通过 `Accept-Language` 偏好的语言，如 `zh-CN`, `en`, `de` |
| `use_cache` | `GLENS_CACHE` | 关闭 | 是否启用本地结果缓存 |
| — | `GLENS_CACHE_TTL` | `86400` | 缓存有效期（秒），默认 24 小时 |
| — | `GLENS_CACHE_DIR` | `~/.cache/glens/results` | 本地缓存目录（Docker 默认 `/data/results`） |
| — | `GLENS_CACHE_DHASH_THRESHOLD` | `4` | L2 感知哈希（64-bit dHash）汉明距离阈值，`0` 为关闭 L2 |
| — | `GLENS_CACHE_MAX_ENTRIES` | `10000` | 本地缓存最大条数，超出自动淘汰最旧条目 |
| — | `GLENS_MAX_IMAGE_EDGE` | `1024` | 上传图片归一化最大长边像素，`0` 为禁用缩放 |
| — | `GLENS_RESOLVE_MAX` | `5` | `resolve_links=True` 最多并发解析的链接数 |
| — | `GLENS_HEADLESS` | `1` | 浏览器模式是否使用无头模式（Docker 中使用 Xvfb 默认 `0`） |
| — | `GLENS_CHROME_PROFILE` | *(空)* | Chrome 持久化用户数据目录（Docker 默认 `/data/chrome-profile`） |
| — | `GLENS_VERCEL_PROXY_URL` / `GLENS_VERCEL_PROXY_URLS` | *(空)* | 单个或多个逗号分隔的 Vercel 代理 HTTPS 地址 |
| — | `GLENS_VERCEL_PROXY_SECRET` / `GLENS_VERCEL_PROXY_SECRETS` | *(空)* | Vercel 代理鉴权密钥（与 Vercel 侧 `CRON_SECRET` 一致） |
| — | `GLENS_PROXY_FALLBACK` | `1` | Vercel 限流/异常/冷却时是否自动分流降级到本地通道 |
| — | `GLENS_VERCEL_RPM` | `50` (Compose) / `20` (代码默认) | 单个 Vercel 槽位每分钟最大请求数（`0` 为不限） |
| — | `GLENS_LOCAL_RPM` | `30` (Compose) / `15` (代码默认) | 单个本地槽位每分钟最大请求数（`0` 为不限） |
| — | `GLENS_SLOT_MIN_INTERVAL` | `0.3` (Compose) / `0.5` (代码默认) | 单槽位两次请求之间的最小间隔（秒） |
| — | `GLENS_PROXY` / `GLENS_LOCAL_PROXIES` | *(空)* | 本地通道（含 Chromium 续期）的单个或多个出口代理 URL |
| — | `GLENS_ALLOW_FAKE_IP` | `0` | 仅本机 Clash Fake-IP 调试用：设为 `1` 时放行 `198.18.0.0/15` |
| — | `GLENS_LOG_LEVEL` | `INFO` | 服务端日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| — | `GLENS_LOG_FILE` | `/data/logs/glens.log` (Docker) | 轮转日志文件路径（为空时仅输出到 stderr） |
| — | `GLENS_LOG_MAX_BYTES` | `20971520` | 单个日志文件上限（默认 20 MiB） |
| — | `GLENS_LOG_BACKUP_COUNT` | `5` | 日志轮转保留的历史文件份数 |

### 语言偏好设置

`lang` 参数通过快速通道的 `Accept-Language` 请求头告诉 Google 偏好的响应语言：

```python
glens.search("img.jpg", lang="zh-CN")   # 偏好中文结果
```

*注：该选项为尽力而为（Best-effort），具体结果语言由 Google 决定，且结果区域始终遵循你的公网出口 IP。（本项目特意未添加 `hl`/`gl` 的 URL 参数篡改，因为 Lens 的结果 URL 经过会话签名，篡改参数会导致 Google 直接返回 403 错误）。*

### 结果缓存机制

设置 `use_cache=True`（或 CLI 参数 `--cache`、环境变量 `GLENS_CACHE=1`）会按图片内容 SHA-256 + 语言作为 Key，将抓取结果缓存在 `~/.cache/glens/results` 下，默认保存 24 小时。在开发调试或重复处理相同图片时非常实用，避免重复向 Google 上传。

---

## 常见排错

- **首次运行返回“无结果”**：首次查询需要 Chrome 来建立会话。请确认已安装拓展依赖：`pip install "chrome-lens-search[browser]"`。
- **完全没有安装 Chrome 的服务器如何运行？**：在任意装有 Chrome 的机器上运行一次，把生成的 Cookie 文件（`~/.cache/glens/cookies.json`）复制到服务器，配置环境变量 `GLENS_COOKIE_FILE` 指向该文件，并使用 `backend="req"`。
- **想查看详细请求进度？**：在 CLI 中添加 `-v` 参数，或在 Python 中配置日志等级：`logging.basicConfig(level=logging.INFO)`。

---

## 注意事项与免责声明

- **该库抓取的是 Google 未公开的接口**：这不是官方 API，且可能违反 Google 服务条款。请仅在合规及个人学习研究场景下使用。
- **接口可能随 Google 前端更新而失效**：Google 会随时调整端点、JS 验证逻辑及页面结构。本项目在解析时故意采用了“轻选择器”（Selector-light，只抓取链接与 aria-label，不绑定混淆的 class 名）策略以保证容错率，但仍需做好维护适配的准备。
- **结果带有一定噪声**：解析器优先保证召回率（Recall）；如有需要，请在下游业务中自行过滤清洗。
- **请友好调用**：单次查询一张图片，库内已加锁保护，切勿进行高频或并发轰炸。

---

## 进阶文档

- **[使用指南 (Guide)](docs/GUIDE.md)**：包含快慢架构深剖、代码自动化、Docker 与无 Chrome 部署方案、CLI 深度指南等。
- **[API 参考 (API Reference)](docs/API.md)**：包含所有函数、数据类、环境变量、CLI 参数与退出码的详细定义。
- **[最佳实践 (Best Practices)](docs/BEST_PRACTICES.md)**：后端选择、凭据管理、缓存策略、安全性及测试指导。

---

## 本地开发与测试

```bash
pip install -e ".[dev]"
pytest          # 完全隔离测试：无需网络，无需浏览器
ruff check .
```

---

## 许可证

MIT License — 详见 [LICENSE](LICENSE)。
