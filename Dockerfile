FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libc6 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libstdc++6 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
        xdg-utils \
    && pip install --upgrade pip \
    && pip install -r /app/requirements.txt \
    && python -m playwright install chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app/app.py
COPY README.md /app/README.md

RUN mkdir -p /app/data

ENV APP_API_KEY=change-me \
    HEADLESS=true \
    PLAYWRIGHT_TIMEOUT_MS=25000 \
    BROWSER_IDLE_TTL_SECONDS=3600 \
    NOMINATIM_URL=https://nominatim.openstreetmap.org/search \
    OPENMAPAPI_REVERSE_URL=https://nominatim.openstreetmap.org/reverse \
    COUNTRYCODES=jp \
    NOMINATIM_UA="gmaps-resolver/1.0 (contact: you@example.com)" \
    CACHE_DB_PATH=/app/data/gmaps_resolver.db \
    CACHE_TTL_SECONDS=2592000 \
    RATE_LIMIT_MAX_REQUESTS=30 \
    RATE_LIMIT_WINDOW_SECONDS=60

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
