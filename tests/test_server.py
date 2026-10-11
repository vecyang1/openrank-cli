"""Tests for OpenTheRank zero-dependency HTTP & REST API server."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
import urllib.request
from http.client import HTTPResponse
from pathlib import Path

from openrank.server import start_server


class TestOpenRankServer(unittest.TestCase):
    """Integration test suite for OpenRank HTTP server and REST endpoints."""

    server = None
    thread = None
    base_url = ""
    temp_dir = None

    @classmethod
    def setUpClass(cls):
        """Spin up the server on an ephemeral port in an isolated temporary environment."""
        cls.temp_dir = tempfile.mkdtemp(prefix="openrank_srv_test_")
        cls.config_dir = Path(cls.temp_dir) / "config"
        cls.cache_dir = Path(cls.temp_dir) / "cache"
        cls.config_dir.mkdir(parents=True, exist_ok=True)
        cls.cache_dir.mkdir(parents=True, exist_ok=True)

        os.environ["OPENRANK_CONFIG_DIR"] = str(cls.config_dir)
        os.environ["OPENRANK_CACHE_DIR"] = str(cls.cache_dir)

        # Seed test cache with high-fidelity fixture data for fast deterministic execution
        from openrank.cache import CacheManager
        from openrank.models import ProductSummary
        from openrank.pricing import PricingEngine

        cache = CacheManager(cache_dir=cls.cache_dir)
        fixture_path = Path(__file__).parent / "fixtures" / "chatgpt_pricing.html"
        if fixture_path.is_file():
            html_content = fixture_path.read_text(encoding="utf-8")
            engine = PricingEngine()

            p_chatgpt = ProductSummary(
                name="ChatGPT Plus",
                slug="chatgpt",
                path="/pricing/chatgpt/",
                category="AI Subscriptions",
                url="https://opentherank.com/pricing/chatgpt/",
            )
            res_chatgpt = engine.parse_pricing_html(html_content, p_chatgpt)
            cache.set("pricing_chatgpt", res_chatgpt.to_dict())

        # Port 0 selects an ephemeral free port
        cls.server, cls.thread = start_server(host="127.0.0.1", port=0, daemon=True)
        port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        """Shut down the server and clean up temporary test directories."""
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.temp_dir and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        """Helper to send HTTP request and return (status_code, headers_dict, body_bytes)."""
        url = f"{self.base_url}{path}"
        req_headers = headers or {}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_headers = dict(resp.getheaders())
                resp_body = resp.read()
                return resp.status, resp_headers, resp_body
        except urllib.error.HTTPError as e:
            resp_headers = dict(e.headers.items()) if e.headers else {}
            resp_body = e.read()
            e.close()
            return e.code, resp_headers, resp_body

    def test_01_health_probe(self):
        """GET /api/health returns 200 with server status, version, and directories."""
        status, headers, body = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("version", data)
        self.assertEqual(data.get("server"), "OpenTheRank/Python-Stdlib")
        self.assertEqual(data.get("config_dir"), str(self.config_dir))
        self.assertEqual(data.get("cache_dir"), str(self.cache_dir))

    def test_02_static_index_html(self):
        """GET / returns 200 with text/html content for single page dashboard."""
        status, headers, body = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = body.decode("utf-8")
        self.assertIn("<title>OpenTheRank", html)
        self.assertIn("Arbitrage Dashboard", html)
        self.assertIn("pricing-table", html)

    def test_03_catalog_list(self):
        """GET /api/catalog returns products list from catalog."""
        status, _, body = self._request("/api/catalog?limit=5")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("count", data)
        self.assertIn("products", data)
        self.assertLessEqual(len(data["products"]), 5)
        if data["products"]:
            first = data["products"][0]
            self.assertIn("slug", first)
            self.assertIn("name", first)

    def test_04_catalog_search(self):
        """GET /api/catalog/search?q=gemini finds Gemini product."""
        status, _, body = self._request("/api/catalog/search?q=gemini")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertGreater(data.get("count", 0), 0)
        slugs = [p["slug"] for p in data["products"]]
        self.assertIn("gemini", slugs)

    def test_05_catalog_filter_category(self):
        """GET /api/catalog?category=Streaming returns only Streaming products."""
        status, _, body = self._request("/api/catalog?category=Streaming&limit=10")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        for p in data["products"]:
            self.assertEqual(p["category"].lower(), "streaming")

    def test_06_pricing_query_chatgpt(self):
        """GET /api/pricing?product=chatgpt returns multi-region pricing result."""
        status, _, body = self._request("/api/pricing?product=chatgpt")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["product"]["slug"], "chatgpt")
        self.assertIn("records", data)
        self.assertGreater(len(data["records"]), 0)
        # Check that records have required fields
        rec = data["records"][0]
        self.assertIn("country_code", rec)
        self.assertIn("primary_tier", rec)
        self.assertIn("usd_price", rec["primary_tier"])
        self.assertIn("local_price_str", rec["primary_tier"])

    def test_07_pricing_query_missing_param(self):
        """GET /api/pricing without product returns 400 INVALID_ARGUMENT."""
        status, _, body = self._request("/api/pricing")
        self.assertEqual(status, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data.get("error"))
        self.assertEqual(data.get("code"), "INVALID_ARGUMENT")

    def test_08_pricing_query_nonexistent(self):
        """GET /api/pricing?product=nonexistent_xyz returns 404 NOT_FOUND."""
        status, _, body = self._request("/api/pricing?product=nonexistent_xyz_9999")
        self.assertEqual(status, 404)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data.get("error"))
        self.assertEqual(data.get("code"), "NOT_FOUND")

    def test_09_pricing_filter_platform(self):
        """GET /api/pricing?product=chatgpt&platform=appstore filters records."""
        status, _, body = self._request("/api/pricing?product=chatgpt&platform=appstore")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        for rec in data["records"]:
            self.assertEqual(rec["platform"], "appstore")

    def test_10_apple_validation(self):
        """GET /api/validate/apple?product=chatgpt&country=us returns validation result."""
        status, _, body = self._request("/api/validate/apple?product=chatgpt&country=us")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("product_slug"), "chatgpt")
        self.assertEqual(data.get("country_code"), "us")
        self.assertIn("items", data)

    def test_11_apple_validation_missing_param(self):
        """GET /api/validate/apple without product returns 400 INVALID_ARGUMENT."""
        status, _, body = self._request("/api/validate/apple")
        self.assertEqual(status, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data.get("error"))
        self.assertEqual(data.get("code"), "INVALID_ARGUMENT")

    def test_12_settings_get_and_post(self):
        """GET and POST /api/settings reads and updates persistent configuration."""
        # 1. Read default settings
        status, _, body = self._request("/api/settings")
        self.assertEqual(status, 200)
        initial = json.loads(body.decode("utf-8"))
        self.assertEqual(initial.get("base_currency"), "USD")

        # 2. Update settings
        update_payload = {
            "base_currency": "EUR",
            "default_country": "PH",
            "cache_ttl_hours": 48,
        }
        status, _, body = self._request("/api/settings", method="POST", data=update_payload)
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertTrue(res.get("success"))
        saved = res.get("settings")
        self.assertEqual(saved.get("base_currency"), "EUR")
        self.assertEqual(saved.get("default_country"), "PH")
        self.assertEqual(saved.get("cache_ttl_hours"), 48)

        # 3. Read again to assert persistence
        status, _, body = self._request("/api/settings")
        self.assertEqual(status, 200)
        reloaded = json.loads(body.decode("utf-8"))
        self.assertEqual(reloaded.get("base_currency"), "EUR")
        self.assertEqual(reloaded.get("default_country"), "PH")

    def test_13_watchlist_management(self):
        """POST /api/watchlist adds and removes slugs, persisting to disk."""
        # Initial empty watchlist
        status, _, body = self._request("/api/watchlist")
        self.assertEqual(status, 200)
        initial = json.loads(body.decode("utf-8"))
        self.assertEqual(initial.get("watchlist"), [])

        # Add 'gemini'
        status, _, body = self._request("/api/watchlist", method="POST", data={"slug": "gemini"})
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("gemini", res.get("watchlist"))

        # Add 'netflix'
        status, _, body = self._request("/api/watchlist", method="POST", data={"slug": "netflix"})
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("gemini", res.get("watchlist"))
        self.assertIn("netflix", res.get("watchlist"))

        # Toggle 'gemini' off
        status, _, body = self._request("/api/watchlist", method="POST", data={"slug": "gemini"})
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertNotIn("gemini", res.get("watchlist"))
        self.assertIn("netflix", res.get("watchlist"))

    def test_14_history_management(self):
        """Querying pricing or posting history populates history; DELETE /api/history clears it."""
        # Record entry via POST /api/history
        entry = {"slug": "spotify", "name": "Spotify Premium", "category": "Music"}
        status, _, body = self._request("/api/history", method="POST", data=entry)
        self.assertEqual(status, 200)

        status, _, body = self._request("/api/history")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        history = data.get("history", [])
        self.assertGreater(len(history), 0)
        slugs = [h["slug"] for h in history]
        self.assertIn("spotify", slugs)

        # Clear history
        status, _, body = self._request("/api/history", method="DELETE")
        self.assertEqual(status, 200)

        # Verify cleared
        status, _, body = self._request("/api/history")
        self.assertEqual(status, 200)
        cleared_data = json.loads(body.decode("utf-8"))
        self.assertEqual(cleared_data.get("history"), [])

    def test_15_diagnostics_telemetry(self):
        """GET /api/diagnose returns environment and connectivity telemetry."""
        status, _, body = self._request("/api/diagnose")
        self.assertEqual(status, 200)
        diag = json.loads(body.decode("utf-8"))
        self.assertIn("version", diag)
        self.assertIn("python_version", diag)
        self.assertIn("config_dir", diag)
        self.assertIn("cache_dir", diag)
        self.assertIn("endpoints", diag)
        self.assertIn("opentherank_status", diag)
        self.assertIn("apple_cdn_status", diag)

    def test_16_cors_options(self):
        """OPTIONS request receives 204 No Content with CORS allow headers."""
        status, headers, _ = self._request("/api/catalog", method="OPTIONS")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("GET", headers.get("Access-Control-Allow-Methods", ""))


if __name__ == "__main__":
    unittest.main()
