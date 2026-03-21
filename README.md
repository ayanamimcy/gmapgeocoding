# Google Maps Share Link Resolver

一个基于 FastAPI + Playwright 的服务，用于解析 Google Maps 分享短链，尽量还原最终跳转地址，并输出坐标或可用于地理编码的查询文本。

## 功能

- 解析 Google Maps 分享链接和跳转链接
- 使用 Playwright 驱动 Chromium 等待前端重定向完成
- 浏览器实例会在首次请求后复用，并在空闲 1 小时后自动关闭
- 优先从最终 Google Maps URL 中提取经纬度
- 当只有文本查询时，回退到 OSM Nominatim 做地理编码
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
- `BROWSER_IDLE_TTL_SECONDS`: 浏览器空闲多久后自动关闭，默认 `3600`
- `BROWSER_USER_AGENT`: 浏览器 UA
- `NOMINATIM_URL`: Nominatim 搜索接口地址
- `COUNTRYCODES`: 限制地理编码国家代码，默认 `jp`
- `NOMINATIM_UA`: 访问 Nominatim 时使用的 User-Agent
- `CACHE_DB_PATH`: SQLite 缓存文件路径
- `CACHE_TTL_SECONDS`: 缓存有效期，默认 30 天
- `RATE_LIMIT_MAX_REQUESTS`: 限流窗口内最大请求数
- `RATE_LIMIT_WINDOW_SECONDS`: 限流窗口时长

## Git 初始化

如果你是从压缩包或目录直接接手这个项目，可以执行：

```bash
git init -b main
git add .
git commit -m "chore: initialize project"
```
