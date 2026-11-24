# Google Maps Share Link Resolver

一个基于 FastAPI + Playwright 的服务，用于解析 Google Maps 分享短链，尽量还原最终跳转地址，并输出坐标或可用于地理编码的查询文本。

## 功能

- 解析 Google Maps 分享链接和跳转链接
- 对 `maps.app.goo.gl` 短链先尝试 HTTP 重定向；只有目标 URL 包含可确认的目的地坐标才走快路径，否则回退到 Playwright
- 浏览器实例会在服务启动时立即拉起，并每 2 小时自动重启一次
- 从显式图钉或单一地点记录提取目的地坐标，不把 `@lat,lon`、`center` 或 `ll` 视口坐标当作目的地
- 当只有文本查询时，回退到 OSM Nominatim 做地理编码
- 解析成功后会基于坐标调用 OpenMap 反解析接口，并将结果落到 SQLite
- 基于 `X-API-Key` 做简单鉴权
- 内置 SQLite 缓存和按 IP + API Key 的限流

## 项目结构

```text
.
|-- app.py
|-- requirements.txt
|-- Dockerfile
|-- docker-compose.yml
|-- env.sample
`-- data/
```

## 环境要求

- Python 3.11+
- Chromium 运行依赖
- Docker / Docker Compose（可选）

## 本地运行

1. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

在部分 Linux 环境下还需要额外依赖：

```bash
python -m playwright install-deps chromium
```

2. 准备环境变量

```bash
cp env.sample .env
```

`.env` 使用 `KEY=VALUE` 格式，编辑完成后可这样加载：

```bash
set -a
source .env
set +a
```

3. 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## Docker 运行

```bash
cp env.sample .env
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

## API 示例

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

解析链接：

```bash
curl -X POST 'http://127.0.0.1:8000/resolve' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: replace-with-a-long-random-string' \
  -d '{"url":"https://maps.app.goo.gl/zKuKtHAq7LbBryPf9?g_st=ic"}'
```

## 环境变量说明

- `APP_API_KEY`: API 鉴权密钥，不能为空
- `HEADLESS`: 是否以无头模式启动浏览器，默认 `true`
- `PLAYWRIGHT_TIMEOUT_MS`: Playwright 页面加载超时时间
- `HTTP_FAST_RESOLVE`: 短链 HTTP 快路径，默认 `true`；设为 `false` 可用相同坐标规则对比只走 Playwright 的耗时
- `HTTP_FAST_RESOLVE_TIMEOUT_SECONDS`: 快路径的 HTTP 超时预算，默认 `2` 秒；最多发出 3 次 HTTP 请求
- `BROWSER_RESTART_INTERVAL_SECONDS`: 浏览器进程重启间隔，默认 `7200`
- `BROWSER_USER_AGENT`: 浏览器 UA
- `NOMINATIM_URL`: Nominatim 搜索接口地址
- `OPENMAPAPI_REVERSE_URL`: OpenMap 反解析接口地址，默认可使用 Nominatim `/reverse`
- `COUNTRYCODES`: 限制地理编码国家代码，默认 `jp`
- `NOMINATIM_UA`: 访问 Nominatim 时使用的 User-Agent
- `CACHE_DB_PATH`: SQLite 缓存文件路径
- `CACHE_TTL_SECONDS`: 缓存有效期，默认 30 天
- `RATE_LIMIT_MAX_REQUESTS`: 限流窗口内最大请求数
- `RATE_LIMIT_WINDOW_SECONDS`: 限流窗口时长

## 解析耗时

每次进入 `/resolve` 处理函数后，服务会输出一条 `resolve timings=` JSON 日志。`total_ms` 是处理函数耗时，不含鉴权和响应传输；`stages_ms` 只包含实际执行的阶段（即使阶段失败也会计时）：

- `http_expand`：尝试通过 HTTP 重定向展开短链；命中时响应的 `method` 为 `direct_google_http_redirect`，`title` 为 `null`（未打开网页）。
- `browser_worker_start` / `browser_queue` / `browser_prepare`：检查或启动 worker、等待排队、准备或重启浏览器。
- `browser_context` / `browser_navigation` / `browser_wait`：创建和关闭页面上下文、打开链接直到 DOM 就绪、处理弹窗并等待最终 URL。
- `forward_geocoding` / `reverse_geocoding`：上游正查和反查耗时。
- `sqlite`：结果写入和当次触发的清理耗时。

计时日志不包含 URL 或地址，原有请求及结果日志不变。

## Git 初始化

如果你是从压缩包或目录直接接手这个项目，可以执行：

```bash
git init -b main
git add .
git commit -m "chore: initialize project"
```
