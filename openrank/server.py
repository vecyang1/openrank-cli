"""Zero-dependency HTTP & REST API server for OpenTheRank Web Dashboard."""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openrank import __version__
from openrank.cache import CacheManager
from openrank.catalog import CatalogError, CatalogManager
from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
)
from openrank.models import (
    AppleValidationResult,
    ProductPricingResult,
    ProductSummary,
)
from openrank.pricing import PricingEngine
from openrank.storefront_apple import (
    KNOWN_APP_IDS,
    AppleStorefrontValidator,
)

WEB_DIR = Path(__file__).resolve().parent / "web"


def get_config_dir() -> Path:
    """Return user configuration directory for OpenRank."""
    default_dir = Path.home() / ".config" / "openrank"
    path = Path(os.environ.get("OPENRANK_CONFIG_DIR", default_dir))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    """Return cache directory for OpenRank."""
    default_dir = Path.home() / ".cache" / "openrank"
    path = Path(os.environ.get("OPENRANK_CACHE_DIR", default_dir))
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> Dict[str, Any]:
    """Load persisted user settings."""
    settings_file = get_config_dir() / "settings.json"
    defaults = {
        "base_currency": "USD",
        "default_platform": "all",
        "default_country": "US",
        "cache_ttl_hours": 24,
        "theme": "dark",
        "auto_apple_validate": False,
    }
    if settings_file.is_file():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
    return defaults


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Persist user settings to disk."""
    current = load_settings()
    current.update(settings)
    settings_file = get_config_dir() / "settings.json"
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


def load_history() -> List[Dict[str, Any]]:
    """Load query history from disk."""
    history_file = get_cache_dir() / "history.json"
    if history_file.is_file():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def record_history_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Record a query entry into persistent history (most recent first, capped at 50)."""
    history = load_history()
    # Deduplicate by slug if present
    slug = entry.get("slug")
    if slug:
        history = [h for h in history if h.get("slug") != slug]
    history.insert(0, entry)
    history = history[:50]
    history_file = get_cache_dir() / "history.json"
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return history


def clear_history() -> None:
    """Clear query history."""
    history_file = get_cache_dir() / "history.json"
    if history_file.is_file():
        try:
            history_file.unlink()
        except Exception:
            pass


def load_watchlist() -> List[str]:
    """Load bookmarked product slugs."""
    watchlist_file = get_config_dir() / "watchlist.json"
    if watchlist_file.is_file():
        try:
            with open(watchlist_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def toggle_watchlist(slug: str) -> List[str]:
    """Toggle a product slug in watchlist."""
    items = set(load_watchlist())
    if slug in items:
        items.remove(slug)
    else:
        items.add(slug)
    result = sorted(list(items))
    watchlist_file = get_config_dir() / "watchlist.json"
    try:
        with open(watchlist_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except Exception:
        pass
    return result


class OpenRankApiHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST APIs and static Dashboard SPA."""

    def send_json(self, data: Any, status: int = 200) -> None:
        """Send a JSON HTTP response with appropriate headers."""
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, code: str = "ERROR", status: int = 400) -> None:
        """Send a structured JSON error response."""
        self.send_json({"error": True, "message": message, "code": code}, status=status)

    def do_OPTIONS(self) -> None:
        """Handle CORS pre-flight requests."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests for static files and REST endpoints."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # 1. Health Probe
        if path == "/api/health":
            self.send_json({
                "status": "ok",
                "version": __version__,
                "server": "OpenTheRank/Python-Stdlib",
                "config_dir": str(get_config_dir()),
                "cache_dir": str(get_cache_dir()),
            })
            return

        # 2. Catalog & Search
        if path in ("/api/catalog", "/api/catalog/search"):
            query = params.get("q", [""])[0].strip()
            category = params.get("category", [None])[0]
            limit_str = params.get("limit", ["20"])[0]
            limit = int(limit_str) if limit_str.isdigit() else 20
            refresh = params.get("refresh", ["false"])[0].lower() in ("true", "1")

            cat_mgr = CatalogManager()
            try:
                if query:
                    results = cat_mgr.search(query=query, category=category, limit=limit, refresh=refresh)
                    products = [
                        {
                            "name": p.name,
                            "slug": p.slug,
                            "path": p.path,
                            "category": p.category,
                            "url": p.url,
                            "score": score,
                        }
                        for p, score in results
                    ]
                else:
                    all_products = cat_mgr.get_catalog(refresh=refresh)
                    if category:
                        all_products = [p for p in all_products if p.category.lower() == category.lower()]
                    products = [p.to_dict() for p in all_products[:limit]]

                self.send_json({
                    "count": len(products),
                    "products": products,
                })
            except Exception as e:
                self.send_error_json(str(e), "CATALOG_ERROR", status=500)
            return

        # 3. Multi-Region Pricing
        if path == "/api/pricing":
            product_slug = params.get("product", [""])[0].strip()
            if not product_slug:
                self.send_error_json("Missing 'product' parameter", "INVALID_ARGUMENT", 400)
                return

            platform = params.get("platform", ["all"])[0].strip().lower()
            country = params.get("country", [None])[0]
            refresh = params.get("refresh", ["false"])[0].lower() in ("true", "1")
            validate_apple = params.get("validate_apple", ["false"])[0].lower() in ("true", "1")

            cat_mgr = CatalogManager()
            try:
                product = cat_mgr.resolve_product(product_slug, refresh=refresh)
            except Exception as e:
                self.send_error_json(str(e), "CATALOG_ERROR", 500)
                return

            if not product:
                # Check known Apple fallback
                if product_slug.lower() in KNOWN_APP_IDS:
                    product = ProductSummary(
                        name=product_slug.title(),
                        slug=product_slug.lower(),
                        path=f"/pricing/{product_slug.lower()}/",
                        category="Apps",
                        url=f"https://opentherank.com/pricing/{product_slug.lower()}/",
                    )
                else:
                    self.send_error_json(f"Product '{product_slug}' not found", "NOT_FOUND", 404)
                    return

            cache_mgr = CacheManager(cache_dir=get_cache_dir())
            cache_key = f"pricing_{product.slug}"
            settings = load_settings()
            ttl_seconds = int(settings.get("cache_ttl_hours", 24)) * 3600

            cached_data = None if refresh else cache_mgr.get(cache_key)
            if cached_data and isinstance(cached_data, dict):
                records = list(cached_data.get("records", []))
                if platform != "all":
                    records = [r for r in records if r.get("platform", "").lower() == platform]
                if country:
                    records = [r for r in records if r.get("country_code", "").upper() == country.upper()]
                res_dict = dict(cached_data)
                res_dict["records"] = records
                record_history_entry({
                    "slug": product.slug,
                    "name": product.name,
                    "category": product.category,
                    "timestamp": str(records[0].get("snapshot_date", "") if records else ""),
                })
                self.send_json(res_dict)
                return

            pricing_engine = PricingEngine()
            try:
                full_result = pricing_engine.fetch_pricing(
                    product=product,
                    platform="all",
                    country=None,
                )
                full_dict = full_result.to_dict()
                cache_mgr.set(cache_key, full_dict, ttl=ttl_seconds)

                records = list(full_dict.get("records", []))
                if platform != "all":
                    records = [r for r in records if r.get("platform", "").lower() == platform]
                if country:
                    records = [r for r in records if r.get("country_code", "").upper() == country.upper()]
                filtered_dict = dict(full_dict)
                filtered_dict["records"] = records

                if validate_apple:
                    target_country = (country or "US").upper()
                    validator = AppleStorefrontValidator()
                    validator.cross_validate_pricing(full_result, target_country)
                    filtered_dict["first_party_validated"] = full_result.first_party_validated
                    filtered_dict["validation_source"] = full_result.validation_source
                    filtered_dict["validation_notes"] = list(full_result.validation_notes)

                record_history_entry({
                    "slug": product.slug,
                    "name": product.name,
                    "category": product.category,
                    "timestamp": str(records[0].get("snapshot_date", "") if records else ""),
                })
                self.send_json(filtered_dict)
                return
            except HttpFastFailError as e:
                self.send_error_json(e.message, "FAST_FAIL_ERROR", e.status_code)
                return
            except (NetworkError, OSError) as e:
                stale_data = cache_mgr.get(cache_key)
                if stale_data and isinstance(stale_data, dict):
                    self.send_json(stale_data)
                    return
                self.send_error_json(str(e), "NETWORK_ERROR", 502)
                return

        # 4. Apple Storefront Validation
        if path == "/api/validate/apple":
            product_slug = params.get("product", [""])[0].strip()
            if not product_slug:
                self.send_error_json("Missing 'product' parameter", "INVALID_ARGUMENT", 400)
                return
            country = params.get("country", ["us"])[0].strip().lower()

            validator = AppleStorefrontValidator()
            val_result = validator.validate_product(product_slug, country)
            self.send_json(val_result.to_dict())
            return

        # 5. User Settings
        if path == "/api/settings":
            self.send_json(load_settings())
            return

        # 6. History
        if path == "/api/history":
            self.send_json({"history": load_history()})
            return

        # 7. Watchlist
        if path == "/api/watchlist":
            self.send_json({"watchlist": load_watchlist()})
            return

        # 8. Diagnostics
        if path == "/api/diagnose":
            diag = {
                "version": __version__,
                "python_version": sys.version,
                "config_dir": str(get_config_dir()),
                "cache_dir": str(get_cache_dir()),
                "endpoints": {
                    "opentherank": "https://opentherank.com",
                    "apple_cdn": "https://apps.apple.com",
                },
            }
            # Fast ping check to OpenTheRank
            client = HttpClient(timeout=2.0)
            try:
                code, _, _ = client.get("https://opentherank.com")
                diag["opentherank_status"] = f"HTTP {code} (ONLINE)"
            except Exception as e:
                diag["opentherank_status"] = f"OFFLINE ({e})"

            # Fast ping check to Apple Storefront
            try:
                code, _, _ = client.get("https://apps.apple.com/us/app/chatgpt/id6448311069")
                diag["apple_cdn_status"] = f"HTTP {code} (ONLINE)"
            except Exception as e:
                diag["apple_cdn_status"] = f"OFFLINE ({e})"

            self.send_json(diag)
            return

        # 9. Static File Serving (Web Dashboard SPA)
        index_file = WEB_DIR / "index.html"
        if not index_file.is_file():
            self.send_error_json("Web dashboard assets not found", "NOT_FOUND", 404)
            return

        try:
            with open(index_file, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error_json(str(e), "SERVER_ERROR", 500)

    def do_POST(self) -> None:
        """Handle POST requests for settings, history, and watchlist."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_error_json("Invalid JSON body", "BAD_REQUEST", 400)
            return

        if path == "/api/settings":
            saved = save_settings(data)
            self.send_json({"success": True, "settings": saved})
            return

        if path == "/api/history":
            updated = record_history_entry(data)
            self.send_json({"success": True, "history": updated})
            return

        if path == "/api/watchlist":
            slug = data.get("slug")
            if not slug:
                self.send_error_json("Missing 'slug'", "BAD_REQUEST", 400)
                return
            updated = toggle_watchlist(slug)
            self.send_json({"success": True, "watchlist": updated})
            return

        self.send_error_json("Endpoint not found", "NOT_FOUND", 404)

    def do_DELETE(self) -> None:
        """Handle DELETE requests for history."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/history":
            clear_history()
            self.send_json({"success": True, "message": "History cleared"})
            return

        self.send_error_json("Endpoint not found", "NOT_FOUND", 404)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stdout access logging for test cleanliness."""
        pass


def start_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    daemon: bool = False,
) -> Tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the OpenRank HTTP and REST API server in a background thread."""
    server = ThreadingHTTPServer((host, port), OpenRankApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=daemon)
    thread.start()
    return server, thread
