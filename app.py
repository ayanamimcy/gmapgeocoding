import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, parse_qs, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request as HTTPRequest, build_opener

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def default_reverse_geocode_url(search_url: str) -> str:
    normalized = search_url.rstrip("/")
    if normalized.endswith("/search"):
        return f"{normalized[:-len('/search')]}/reverse"
    return f"{normalized}/reverse"


APP_API_KEY = os.getenv("APP_API_KEY", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "25000"))
HTTP_FAST_RESOLVE = os.getenv("HTTP_FAST_RESOLVE", "true").lower() != "false"
HTTP_FAST_RESOLVE_TIMEOUT_SECONDS = float(os.getenv("HTTP_FAST_RESOLVE_TIMEOUT_SECONDS", "2"))
BROWSER_RESTART_INTERVAL_SECONDS = int(os.getenv("BROWSER_RESTART_INTERVAL_SECONDS", "7200"))
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OPENMAPAPI_REVERSE_URL = os.getenv(
    "OPENMAPAPI_REVERSE_URL",
    default_reverse_geocode_url(NOMINATIM_URL),
)
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
logger = logging.getLogger("uvicorn.error")


class ResolveRequest(BaseModel):
    url: str = Field(..., description="Google Maps share URL")


class ReverseGeocodeResult(BaseModel):
    source: str
    display_name: Optional[str] = None
    address: Optional[dict[str, Any]] = None
    raw: Optional[dict[str, Any]] = None


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
    reverse_geocode: Optional[ReverseGeocodeResult] = None
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reverse_geocode_results (
                url_hash TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                final_url TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                reverse_geocode_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires_at ON cache_entries(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_ts ON rate_limit_events(bucket_key, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reverse_geocode_lat_lon ON reverse_geocode_results(lat, lon)")
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
    browser_manager.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    browser_manager.shutdown()


def normalize_url(url: str) -> str:
    return url.strip()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_failed_response(response: dict) -> bool:
    return bool(response.get("error"))


def has_coordinates(response: dict) -> bool:
    return response.get("lat") is not None and response.get("lon") is not None


def to_log_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


@contextmanager
def timed_stage(timings: dict[str, float], stage: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + elapsed_ms(started)


def set_cached_response(url: str, response: dict) -> None:
    if is_failed_response(response):
        return

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


def save_reverse_geocode_result(url: str, response: dict) -> None:
    reverse_geocode = response.get("reverse_geocode")
    if not reverse_geocode or not has_coordinates(response):
        return

    normalized = normalize_url(url)
    url_hash = hash_text(normalized)
    now_ts = int(time.time())

    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO reverse_geocode_results (
                url_hash, url, final_url, lat, lon, reverse_geocode_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                url = excluded.url,
                final_url = excluded.final_url,
                lat = excluded.lat,
                lon = excluded.lon,
                reverse_geocode_json = excluded.reverse_geocode_json,
                updated_at = excluded.updated_at
            """,
            (
                url_hash,
                normalized,
                response["final_url"],
                float(response["lat"]),
                float(response["lon"]),
                json.dumps(reverse_geocode, ensure_ascii=False),
                now_ts,
                now_ts,
            ),
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


GOOGLE_MAPS_HOSTS = frozenset({
    "google.com", "www.google.com", "maps.google.com",
    "google.co.jp", "www.google.co.jp", "maps.google.co.jp",
})
SHORT_MAPS_HOST = "maps.app.goo.gl"
COORDINATE = r"-?[0-9]+(?:\.[0-9]+)?"
COORDINATE_QUERY = re.compile(rf"\s*({COORDINATE})\s*,\s*({COORDINATE})\s*")
PLACE_DATA_PATH = re.compile(r"/maps/place/[^/]+/(?:@[^/]+/)?data=([^/]+)/?")
PLACE_COORDINATES = re.compile(rf"!8m2!3d({COORDINATE})!4d({COORDINATE})(?=!|$)")


def parse_google_maps_url(url: str) -> Optional[SplitResult]:
    if "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    # Match the entire authority: no credentials, custom ports, or lookalike hosts.
    host = parsed.netloc.lower()
    if host == SHORT_MAPS_HOST and re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
        return parsed
    if host in GOOGLE_MAPS_HOSTS and (parsed.path == "/maps" or parsed.path.startswith("/maps/")):
        return parsed
    return None


def valid_coordinates(lat: str, lon: str) -> Optional[Tuple[float, float]]:
    coordinates = float(lat), float(lon)
    if -90 <= coordinates[0] <= 90 and -180 <= coordinates[1] <= 180:
        return coordinates
    return None


def extract_search_query(parsed: SplitResult) -> Optional[str]:
    if parsed.path.rstrip("/") not in {"/maps", "/maps/search"}:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if {"query_place_id", "place_id", "cid", "destination", "map_action"} & query.keys():
        return None
    values = query.get("query", []) + query.get("q", [])
    return (values[0].strip() or None) if len(values) == 1 else None


def extract_latlon_from_url(url: str) -> Optional[Tuple[float, float]]:
    parsed = parse_google_maps_url(url)
    if parsed is None or parsed.hostname == SHORT_MAPS_HOST:
        return None

    # A single place record can carry both a place pin and an unrelated @ viewport.
    path = PLACE_DATA_PATH.fullmatch(parsed.path)
    if path:
        data = unquote(path.group(1))
        identifiers = list(re.finditer(r"!1s[^!]+", data))
        pairs = list(PLACE_COORDINATES.finditer(data))
        if (len(identifiers) == len(pairs) == 1 and identifiers[0].end() <= pairs[0].start()
                and data.count("!3d") == data.count("!4d") == 1):
            return valid_coordinates(*pairs[0].groups())
        return None

    query = extract_search_query(parsed)
    coordinates = COORDINATE_QUERY.fullmatch(query) if query else None
    return valid_coordinates(*coordinates.groups()) if coordinates else None


def extract_text_query(url: str) -> Optional[str]:
    parsed = parse_google_maps_url(url)
    query = extract_search_query(parsed) if parsed else None
    return query if query and not COORDINATE_QUERY.fullmatch(query) else None


class NoHttpRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def try_http_resolve(url: str, timings: dict[str, float]) -> Optional[dict]:
    parsed = parse_google_maps_url(url)
    if not HTTP_FAST_RESOLVE or parsed is None or parsed.hostname != SHORT_MAPS_HOST:
        return None

    with timed_stage(timings, "http_expand"):
        opener = build_opener(NoHttpRedirect())
        deadline = time.monotonic() + HTTP_FAST_RESOLVE_TIMEOUT_SECONDS
        visited = [url]
        for _ in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            req = HTTPRequest(visited[-1], headers={
                "User-Agent": "Maps-Link-Resolver/1.1",
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            })
            try:
                with opener.open(req, timeout=remaining):
                    # A page without an HTTP redirect may require JavaScript.
                    return None
            except HTTPError as exc:
                with closing(exc):
                    location = exc.headers.get("Location")
                    if exc.code not in {301, 302, 303, 307, 308} or not location:
                        return None
            except (URLError, OSError, ValueError):
                return None

            try:
                target = urljoin(visited[-1], location)
            except ValueError:
                return None
            if parse_google_maps_url(target) is None or target in visited:
                return None
            visited.append(target)
            coordinates = extract_latlon_from_url(target)
            if coordinates:
                result = base_resolve_result(url, target, visited)
                result.update(method="direct_google_http_redirect", lat=coordinates[0], lon=coordinates[1])
                return result
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
    if not data or data.get("error"):
        return None
    first = data[0]
    return {
        "lat": float(first["lat"]),
        "lon": float(first["lon"]),
        "display_name": first.get("display_name"),
        "raw": first,
    }


def openmapapi_reverse_geocode(lat: float, lon: float) -> Optional[dict]:
    r = requests.get(
        OPENMAPAPI_REVERSE_URL,
        params={
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "addressdetails": 1,
            "accept-language": "ja,en",
        },
        headers={"User-Agent": NOMINATIM_UA},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None

    return {
        "source": "openmapapi",
        "display_name": data.get("display_name"),
        "address": data.get("address"),
        "raw": data,
    }


def enrich_successful_response(url: str, response: dict, timings: dict[str, float]) -> dict:
    if not has_coordinates(response):
        return response

    enriched = dict(response)
    if not enriched.get("reverse_geocode"):
        with timed_stage(timings, "reverse_geocoding"):
            reverse_geocode = openmapapi_reverse_geocode(
                float(enriched["lat"]),
                float(enriched["lon"]),
            )
        if reverse_geocode:
            enriched["reverse_geocode"] = reverse_geocode

    with timed_stage(timings, "sqlite"):
        save_reverse_geocode_result(url, enriched)
    return enriched


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


@dataclass
class ResolveJob:
    url: str
    timings: dict[str, float]
    queued_at: float


class BrowserManager:
    def __init__(self, restart_interval_seconds: int):
        self.restart_interval_seconds = restart_interval_seconds
        self._jobs: queue.Queue[tuple[str, Optional[ResolveJob], Optional[Future]]] = queue.Queue()
        self._stop_event = threading.Event()
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._browser_active = False
        self._browser_started_at: Optional[int] = None
        self._browser_started_monotonic: Optional[float] = None
        self._last_used_at: Optional[int] = None

    def start(self) -> None:
        warmup_future: Optional[Future] = None
        with self._start_lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="playwright-browser-manager",
                daemon=True,
            )
            self._worker.start()
            warmup_future = Future()
            self._jobs.put(("warmup", None, warmup_future))

        warmup_future.result(timeout=60)

    def shutdown(self) -> None:
        worker = None
        with self._start_lock:
            worker = self._worker
            if not worker:
                return
            self._stop_event.set()
            self._jobs.put(("shutdown", None, None))
            self._worker = None

        worker.join(timeout=10)
        with self._state_lock:
            self._browser_active = False
            self._browser_started_at = None
            self._browser_started_monotonic = None
            self._last_used_at = None

    def resolve(self, url: str, timings: dict[str, float]) -> dict:
        with timed_stage(timings, "browser_worker_start"):
            self.start()
        future: Future = Future()
        self._jobs.put(("resolve", ResolveJob(url, timings, time.perf_counter()), future))
        return future.result()

    def snapshot(self) -> dict:
        with self._state_lock:
            worker_running = bool(self._worker and self._worker.is_alive())
            return {
                "browser_worker_running": worker_running,
                "browser_active": self._browser_active,
                "browser_started_at": self._browser_started_at,
                "browser_last_used_at": self._last_used_at,
            }

    def _mark_browser_open(self) -> None:
        now_monotonic = time.monotonic()
        now_ts = int(time.time())
        with self._state_lock:
            self._browser_active = True
            self._browser_started_at = now_ts
            self._browser_started_monotonic = now_monotonic
            self._last_used_at = now_ts

    def _mark_browser_used(self) -> None:
        now_ts = int(time.time())
        with self._state_lock:
            self._last_used_at = now_ts

    def _mark_browser_closed(self) -> None:
        with self._state_lock:
            self._browser_active = False
            self._browser_started_at = None
            self._browser_started_monotonic = None
            self._last_used_at = None

    def _worker_loop(self) -> None:
        playwright = None
        browser = None

        try:
            while True:
                try:
                    action, payload, future = self._jobs.get(timeout=5)
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    try:
                        browser, playwright = self._restart_browser_if_due(browser, playwright)
                        browser, playwright = self._ensure_browser(browser, playwright)
                    except Exception:
                        browser, playwright = self._close_browser(browser, playwright)
                    continue

                if action == "shutdown":
                    break

                if action not in {"warmup", "resolve"}:
                    continue

                try:
                    if action == "warmup":
                        browser, playwright = self._restart_browser_if_due(browser, playwright)
                        browser, playwright = self._ensure_browser(browser, playwright)
                        if future is not None:
                            future.set_result(None)
                        continue

                    if future is None or payload is None:
                        continue
                    timings = payload.timings
                    timings["browser_queue"] = elapsed_ms(payload.queued_at)
                    with timed_stage(timings, "browser_prepare"):
                        browser, playwright = self._restart_browser_if_due(browser, playwright)
                        browser, playwright = self._ensure_browser(browser, playwright)
                    self._mark_browser_used()
                    result = resolve_with_browser(browser, payload.url, timings)
                    self._mark_browser_used()
                    future.set_result(result)
                except Exception as exc:
                    browser, playwright = self._close_browser(browser, playwright)
                    if future is not None:
                        future.set_exception(exc)
        finally:
            self._close_browser(browser, playwright)

    def _ensure_browser(self, browser: Any, playwright: Any) -> tuple[Any, Any]:
        if browser is not None and playwright is not None:
            return browser, playwright

        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=HEADLESS)
        except Exception:
            playwright.stop()
            raise
        self._mark_browser_open()
        return browser, playwright

    def _restart_browser_if_due(self, browser: Any, playwright: Any) -> tuple[Any, Any]:
        if browser is None or playwright is None:
            return browser, playwright

        with self._state_lock:
            browser_started_monotonic = self._browser_started_monotonic

        if browser_started_monotonic is None:
            return self._close_browser(browser, playwright)

        if (time.monotonic() - browser_started_monotonic) < self.restart_interval_seconds:
            return browser, playwright

        return self._close_browser(browser, playwright)

    def _close_browser(self, browser: Any, playwright: Any) -> tuple[None, None]:
        try:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass
            self._mark_browser_closed()

        return None, None


browser_manager = BrowserManager(restart_interval_seconds=BROWSER_RESTART_INTERVAL_SECONDS)


def resolve_with_browser(browser: Any, url: str, timings: dict[str, float]) -> dict:
    with timed_stage(timings, "browser_context"):
        context = browser.new_context(user_agent=BROWSER_USER_AGENT, locale="ja-JP")
    try:
        with timed_stage(timings, "browser_context"):
            page = context.new_page()
            visited_urls: list[str] = []
            page.on("framenavigated", lambda frame: visited_urls.append(frame.url) if frame == page.main_frame else None)

        with timed_stage(timings, "browser_navigation"):
            page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)

        with timed_stage(timings, "browser_wait"):
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
    finally:
        with timed_stage(timings, "browser_context"):
            context.close()

    result = base_resolve_result(url, final_url, visited_urls, page_title)
    latlon = extract_latlon_from_url(final_url)
    if latlon:
        result["method"] = "direct_google_final_url"
        result["lat"], result["lon"] = latlon
        return result

    text_query = extract_text_query(final_url)
    if text_query:
        result["query"] = text_query
        with timed_stage(timings, "forward_geocoding"):
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
    result["error"] = "No destination coordinates or searchable query found in final URL"
    return result


def base_resolve_result(
    url: str, final_url: str, visited_urls: list[str], page_title: Optional[str] = None
) -> dict:
    return {
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
        "reverse_geocode": None,
        "error": None,
        "cache_hit": False,
        "cached_at": None,
    }


@app.get("/healthz")
def healthz():
    browser_status = browser_manager.snapshot()
    return {
        "ok": True,
        "db_path": CACHE_DB_PATH,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "openmapapi_reverse_url": OPENMAPAPI_REVERSE_URL,
        "rate_limit_window_seconds": RATE_LIMIT_WINDOW_SECONDS,
        "rate_limit_max_requests": RATE_LIMIT_MAX_REQUESTS,
        "browser_restart_interval_seconds": BROWSER_RESTART_INTERVAL_SECONDS,
        **browser_status,
    }


@app.post("/resolve", response_model=ResolveResponse, dependencies=[Depends(verify_api_key_and_rate_limit)])
def resolve(payload: ResolveRequest):
    started = time.perf_counter()
    timings: dict[str, float] = {}
    outcome = "error"
    normalized_url = normalize_url(payload.url)
    log_input = {
        "url": payload.url,
        "normalized_url": normalized_url,
    }
    logger.info("resolve input=%s", to_log_json(log_input))

    try:
        result = try_http_resolve(normalized_url, timings)
        if result is None:
            result = browser_manager.resolve(normalized_url, timings)
        result = enrich_successful_response(normalized_url, result, timings)
        with timed_stage(timings, "sqlite"):
            set_cached_response(normalized_url, result)
        logger.info("resolve output=%s", to_log_json(result))
        if int(time.time()) % 20 == 0:
            with timed_stage(timings, "sqlite"):
                cleanup_db()
        outcome = result["method"]
        return result
    except requests.HTTPError as e:
        logger.exception("resolve upstream_error input=%s", to_log_json(log_input))
        raise HTTPException(status_code=502, detail=f"Upstream geocoding error: {e}")
    except Exception as e:
        logger.exception("resolve error input=%s", to_log_json(log_input))
        raise HTTPException(status_code=500, detail=f"Resolver error: {e}")
    finally:
        logger.info(
            "resolve timings=%s",
            to_log_json({
                "outcome": outcome,
                "total_ms": round(elapsed_ms(started), 2),
                "stages_ms": {stage: round(value, 2) for stage, value in timings.items()},
            }),
        )
