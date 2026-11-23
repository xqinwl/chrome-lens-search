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

在项目目录中启动服务，然后上传图片或提交公网图片 URL。请将示例文件名或 URL 换成实际图片。

```bash
docker compose up -d --build
curl http://localhost:8000/healthz
curl -F 'image=@shoe.jpg' -F 'lang=zh-CN' http://localhost:8000/v1/search
curl -H 'Content-Type: application/json' \
  -d '{"image_url":"https://example.com/shoe.jpg","max_matches":5}' \
  http://localhost:8000/v1/search
```

Compose 根据宿主机架构构建 Linux AMD64 或 ARM64 镜像，内置 Chromium、配套驱动和用于浏览器重试的虚拟显示器。宿主机通过本地 8000 端口访问，同一 Compose 网络内的容器可访问 `http://glens:8000`。接口面向可信内网，默认免鉴权。若宿主机 8000 端口已被占用，可用 `GLENS_PORT=8080 docker compose up -d` 切换宿主机端口。

首次查询按需建立会话。Chrome 先尝试无头模式，Google 返回不完整页面时，会在虚拟显示器中重试。`/data` 命名卷保存 Cookie 和可选的结果缓存，重启或重建容器后仍可复用。要在接入流量前预热，可运行 `docker compose exec glens glens warmup`。`GET /healthz` 用于检查 HTTP 服务是否在线。`GET /v1/status` 显示后端、缓存开关、本地会话文件是否可读及其年龄，以及允许的搜索并发数；会话是否仍被 Google 接受由实际查询验证。

两种输入均支持 `lang`、`max_matches`、`max_titles`、`resolve_links`。单图上限为 10 MiB。远程 URL 和各次重定向都须指向公网 HTTP(S) 地址。成功响应包含 `top_title`、`titles` 和 `matches`，每条匹配含 `title`、`url`、`domain`。确认无匹配时返回 HTTP 200，标题为 null，列表为空。

如果 Docker DNS 将图片域名解析成 Clash Fake-IP 地址（例如 `198.18.x.x`），需要让 Clash 对该域名返回真实公网地址。在 Clash Verge 配置中，将 `cdn.globus.pictures` 加入 `dns.fake-ip-filter`。如果还希望该 CDN 直连，再在兜底代理规则前添加 `DOMAIN,cdn.globus.pictures,DIRECT`。修改 DNS 规则后重建容器，让它重新解析域名。API 会拒绝虚拟、内网和保留 IP，这是服务端防止请求伪造的安全校验。

requests 后端支持独立搜索并发，服务端内置路由池（`RoutePool`）与控频熔断机制。错误格式为 `{"error":{"code":"…","message":"…"}}`。413 表示请求过大，415 表示内容格式不受支持，422 表示输入无效。HTTP 429 `upstream_rate_limited` 表示 Google Lens 触发了 429 限流或所有可用出口到达速率上限；HTTP 503 `upstream_bot_challenge` 表示所有可用出口均触发了 Google `/sorry` 机器人检测并进入熔断冷却（两者均会返回 `Retry-After` 头，避免继续撞墙）。502 的错误码区分图片下载失败（`image_download_failed`）与 Lens 查询失败或结果无法判断（`lens_lookup_failed`）。成功响应会附带 `X-Glens-Route` 响应头（如 `cache-l1`、`cache-l2`、`vercel-0`、`local-0`），便于观测缓存命中与路由分流情况。

要通过 Vercel 代理搜索，设置 `GLENS_VERCEL_PROXY_URL`（或逗号分隔的多个区域地址 `GLENS_VERCEL_PROXY_URLS`）和 `GLENS_VERCEL_PROXY_SECRET`，并在 JSON 请求中传 `"proxy":"vercel"`。Docker 部署默认开启 `GLENS_PROXY_FALLBACK=1` 与两级本地缓存（`GLENS_CACHE=1`，含 SHA-256 精确匹配、dHash 近似图去重与同图并发合并）：请求优先走健康的 Vercel 路由并受 `GLENS_VERCEL_RPM` 控频保护；当 Vercel 触发 `/sorry`、429、代理异常或达到单路速率上限时，会自动熔断冷却并无缝分流/降级到本地容器通道（支持 `curl_cffi` HTTP/2 指纹、持久化 Chrome Profile 与按路由隔离的 Cookie 滚动回写）。若需严格禁止回退，可将 `GLENS_PROXY_FALLBACK=0`。所有上传图片默认按长边 `GLENS_MAX_IMAGE_EDGE=1024` 归一化为紧凑 JPEG；如需进一步横向扩容吞吐，可直接通过 `GLENS_VERCEL_PROXY_URLS` 或 `GLENS_LOCAL_PROXIES` 配置多出口，无需改代码。

运维接口：`GET /v1/pool/status` 查看各路由状态、剩余冷却时间、近 1 分钟请求数与缓存命中统计；`POST /v1/warmup`（可选传 `{"slot_id": null, "reset_cooldown": true, "force": true}`）可在不重启容器的情况下重置冷却并刷新会话。

从源码直接运行可使用 `pip install ".[server,browser]"` 和 `glens serve`，默认监听 `127.0.0.1:8000`，可通过 `--host` 和 `--port` 调整。

若 URL 返回 `422 invalid_image_url`，请确认图片域名及每次重定向都解析到公网 IP。`502 image_download_failed` 表示图片地址无法访问或拒绝下载。冷启动后若收到 `502 lens_lookup_failed` 且 `req_ready` 为 `false`，表示尚未建立可用 Lens 会话。可运行 `docker compose exec glens glens warmup -v` 检查浏览器步骤。

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
| — | `GLENS_CACHE_DIR` | `~/.cache/glens/results` | 本地缓存目录 |
| — | `GLENS_HEADLESS` | `1` | 浏览器模式是否使用无头模式 (`1` 为开启) |
| — | `GLENS_CHROME_PROFILE` | *(空)* | Chrome 持久化用户数据目录 (Profile) |

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
