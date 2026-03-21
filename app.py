import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

APP_API_KEY = os.getenv("APP_API_KEY", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "25000"))
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
COUNTRYCODES = os.getenv("COUNTRYCODES", "jp")

CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "./data/gmaps_resolver.db")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "2592000"))  # 30 days
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "30"))

NOMINATIM_UA = os.getenv("NOMINATIM_UA", "gmaps-resolver/1.0 (contact: you@example.com)")
BROWSER_USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

app = FastAPI(title="Google Maps Share Link Resolver", version="1.1.0")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class ResolveRequest(BaseModel):
    url: str = Field(..., description="Google Maps share URL")


class ResolveResponse(BaseModel):
    original_url: str
    final_url: str
    title: Optional[str] = None
    visited_urls: list[str]
    redirect_count_estimate: int
    method: str
    query: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    display_name: Optional[str] = None
    error: Optional[str] = None
    cache_hit: bool = False
    cached_at: Optional[int] = None


def get_db() -> sqlite3.Connection:
    db_dir = os.path.dirname(CACHE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                url_hash TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_key TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires_at ON cache_entries(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_ts ON rate_limit_events(bucket_key, ts)")
        conn.commit()


def cleanup_db(now_ts: Optional[int] = None) -> None:
    if now_ts is None:
        now_ts = int(time.time())
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (now_ts,))
        conn.execute("DELETE FROM rate_limit_events WHERE ts <= ?", (now_ts - RATE_LIMIT_WINDOW_SECONDS,))
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    cleanup_db()


def normalize_url(url: str) -> str:
    return url.strip()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_cached_response(url: str) -> Optional[dict]:
    normalized = normalize_url(url)
    url_hash = hash_text(normalized)
    now_ts = int(time.time())
    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT response_json, created_at
            FROM cache_entries
            WHERE url_hash = ? AND expires_at > ?
            """,
            (url_hash, now_ts),
        ).fetchone()

    if not row:
        return None

    data = json.loads(row["response_json"])
    data["cache_hit"] = True
    data["cached_at"] = row["created_at"]
    return data


def set_cached_response(url: str, response: dict) -> None:
    normalized = normalize_url(url)
    url_hash = hash_text(normalized)
    now_ts = int(time.time())
    expires_at = now_ts + CACHE_TTL_SECONDS

    cacheable = dict(response)
    cacheable["cache_hit"] = False
    cacheable["cached_at"] = now_ts

    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO cache_entries (url_hash, url, response_json, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                url = excluded.url,
                response_json = excluded.response_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (url_hash, normalized, json.dumps(cacheable, ensure_ascii=False), now_ts, expires_at),
        )
        conn.commit()


def verify_api_key_and_rate_limit(
    request: Request, api_key: Optional[str] = Depends(api_key_header)
) -> str:
    if not APP_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server APP_API_KEY is not configured."
        )
    if not api_key or not hmac.compare_digest(api_key, APP_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key."
        )

    now_ts = int(time.time())
    api_hash = hash_text(api_key)
    client_ip = request.client.host if request.client else "unknown"
    bucket_key = f"{api_hash}:{client_ip}"

    with closing(get_db()) as conn:
        conn.execute(
            "DELETE FROM rate_limit_events WHERE ts <= ?",
            (now_ts - RATE_LIMIT_WINDOW_SECONDS,),
        )
        current = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM rate_limit_events
            WHERE bucket_key = ? AND ts > ?
            """,
            (bucket_key, now_ts - RATE_LIMIT_WINDOW_SECONDS),
        ).fetchone()["cnt"]

        if current >= RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded. Max {RATE_LIMIT_MAX_REQUESTS} requests "
                    f"per {RATE_LIMIT_WINDOW_SECONDS} seconds."
                ),
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )

        conn.execute(
            "INSERT INTO rate_limit_events (bucket_key, ts) VALUES (?, ?)",
            (bucket_key, now_ts),
        )
        conn.commit()

    return api_key


def extract_latlon_from_url(url: str) -> Optional[Tuple[float, float]]:
    decoded = unquote(url)

    patterns = [
        r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)',
        r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',
    ]
    for pat in patterns:
        m = re.search(pat, decoded)
        if m:
            return float(m.group(1)), float(m.group(2))

    qs = parse_qs(urlparse(decoded).query)
    for key in ("query", "q", "ll", "center"):
        value = qs.get(key, [None])[0]
        if not value:
            continue
        value = value.strip()
        m = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', value)
        if m:
            return float(m.group(1)), float(m.group(2))

    return None


def extract_text_query(url: str) -> Optional[str]:
    decoded = unquote(url)
    qs = parse_qs(urlparse(decoded).query)
    for key in ("query", "q"):
        value = qs.get(key, [None])[0]
        if value:
            value = value.strip()
            if not re.match(r'^\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*$', value):
                return value
    return None


def nominatim_search(query: str) -> Optional[dict]:
    r = requests.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": COUNTRYCODES,
            "accept-language": "ja,en",
        },
        headers={"User-Agent": NOMINATIM_UA},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    first = data[0]
    return {
        "lat": float(first["lat"]),
        "lon": float(first["lon"]),
        "display_name": first.get("display_name"),
        "raw": first,
    }


def maybe_accept_google_dialogs(page) -> None:
    candidates = [
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Accept')",
        "button:has-text('同意する')",
        "button:has-text('すべて受け入れる')",
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1200):
                locator.click(timeout=1200)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def resolve_with_playwright(url: str) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(user_agent=BROWSER_USER_AGENT, locale="ja-JP")
        page = context.new_page()

        visited_urls: list[str] = []
        page.on("framenavigated", lambda frame: visited_urls.append(frame.url) if frame == page.main_frame else None)

        page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
        maybe_accept_google_dialogs(page)

        last_url = page.url
        deadline = time.time() + (PLAYWRIGHT_TIMEOUT_MS / 1000.0)

        while time.time() < deadline:
            if extract_latlon_from_url(page.url):
                break
            try:
                page.wait_for_load_state("networkidle", timeout=1500)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(700)
            maybe_accept_google_dialogs(page)
            if page.url != last_url:
                last_url = page.url

        final_url = page.url
        page_title = page.title()
        browser.close()

    result = {
        "original_url": url,
        "final_url": final_url,
        "title": page_title,
        "visited_urls": visited_urls,
        "redirect_count_estimate": max(0, len(visited_urls) - 1),
        "method": "",
        "query": None,
        "lat": None,
        "lon": None,
        "display_name": None,
        "error": None,
        "cache_hit": False,
        "cached_at": None,
    }

    latlon = extract_latlon_from_url(final_url)
    if latlon:
        result["method"] = "direct_google_final_url"
        result["lat"], result["lon"] = latlon
        return result

    text_query = extract_text_query(final_url)
    if text_query:
        result["query"] = text_query
        geo = nominatim_search(text_query)
        if geo:
            result["method"] = "google_query_then_nominatim"
            result["lat"] = geo["lat"]
            result["lon"] = geo["lon"]
            result["display_name"] = geo["display_name"]
            return result
        result["method"] = "no_match"
        result["error"] = "Nominatim returned no result for extracted query"
        return result

    result["method"] = "unresolved"
    result["error"] = "No coordinates and no searchable query found in final URL"
    return result


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "db_path": CACHE_DB_PATH,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "rate_limit_window_seconds": RATE_LIMIT_WINDOW_SECONDS,
        "rate_limit_max_requests": RATE_LIMIT_MAX_REQUESTS,
    }


@app.post("/resolve", response_model=ResolveResponse, dependencies=[Depends(verify_api_key_and_rate_limit)])
def resolve(payload: ResolveRequest):
    normalized_url = normalize_url(payload.url)

    cached = get_cached_response(normalized_url)
    if cached:
        return cached

    try:
        result = resolve_with_playwright(normalized_url)
        set_cached_response(normalized_url, result)
        if int(time.time()) % 20 == 0:
            cleanup_db()
        return result
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream geocoding error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resolver error: {e}")
