"""Hermetic unit tests for OpenTheRank CLI: models, client, cache, and catalog search."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openrank.cache import CacheManager
from openrank.catalog import CatalogError, CatalogManager
from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
    NoRedirectHandler,
)
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestModels(unittest.TestCase):
    """Test suite for domain models and dataclasses."""

    def test_product_summary_dataclass(self) -> None:
        p = ProductSummary(
            name="ChatGPT",
            slug="chatgpt",
            path="/ai-pricing/chatgpt/",
            category="AI Subscriptions",
            url="https://opentherank.com/ai-pricing/chatgpt/",
        )
        self.assertEqual(p.name, "ChatGPT")
        self.assertEqual(p.slug, "chatgpt")
        self.assertEqual(p.category, "AI Subscriptions")
        d = p.to_dict()
        self.assertEqual(d["slug"], "chatgpt")
        p2 = ProductSummary.from_dict(d)
        self.assertEqual(p, p2)

    def test_price_tier(self) -> None:
        tier = PriceTier(
            name="Plus",
            local_price_str="≈ ₱999/mo",
            usd_price=15.89,
            delta_percent=-21.0,
        )
        self.assertEqual(tier.name, "Plus")
        self.assertEqual(tier.usd_price, 15.89)
        self.assertEqual(tier.delta_percent, -21.0)
        d = tier.to_dict()
        tier2 = PriceTier.from_dict(d)
        self.assertEqual(tier2.name, tier.name)
        self.assertEqual(tier2.usd_price, tier.usd_price)

    def test_region_pricing_record(self) -> None:
        tier = PriceTier(name="Plus", local_price_str="≈ ₱999/mo", usd_price=15.89)
        record = RegionPricingRecord(
            country_code="PH",
            region_name="Philippines",
            platform="appstore",
            primary_tier=tier,
            vs_us_delta=-21.0,
            tax_info="12% VAT included",
            risk_level="Medium",
            risk_note="May require local payment method",
            snapshot_date="2026-10-11",
        )
        self.assertEqual(record.country_code, "PH")
        self.assertEqual(record.primary_tier.usd_price, 15.89)
        self.assertEqual(record.risk_level, "Medium")
        d = record.to_dict()
        self.assertEqual(d["country_code"], "PH")
        self.assertIn("primary_tier", d)

    def test_product_pricing_result_sync(self) -> None:
        p = ProductSummary("ChatGPT", "chatgpt", "/ai-pricing/chatgpt/", "AI", "https://example.com")
        tier = PriceTier("Plus", "$19.99/mo", 19.99)
        rec = RegionPricingRecord("US", "United States", "appstore", tier)
        result = ProductPricingResult(product=p, records=[rec], baseline_us_price=19.99)

        self.assertEqual(result.baseline_us_price, 19.99)
        self.assertEqual(result.us_baseline_usd, 19.99)
        self.assertIn("appstore", result.platforms)

    def test_apple_iap_item_field_aliases(self) -> None:
        item1 = AppleIAPItem(name="ChatGPT Plus", local_price_str="₱ 999.00", numeric_price=15.89)
        self.assertEqual(item1.tier_name, "ChatGPT Plus")
        self.assertEqual(item1.raw_amount, 15.89)

        item2 = AppleIAPItem(name="", local_price_str="₱ 999.00", tier_name="ChatGPT Pro", raw_amount=103.22)
        self.assertEqual(item2.name, "ChatGPT Pro")
        self.assertEqual(item2.numeric_price, 103.22)

    def test_apple_validation_result(self) -> None:
        val = AppleValidationResult(
            product_slug="chatgpt",
            country_code="xy",
            requested_url="https://apps.apple.com/xy/app/chatgpt/id6448311069",
            final_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            is_spoofed=True,
            items=[],
            error="Redirected to US store",
        )
        self.assertTrue(val.is_spoofed)
        self.assertFalse(val.is_valid)
        self.assertEqual(val.error_message, "Redirected to US store")
        self.assertEqual(val.redirect_target, val.final_url)


class TestHttpClient(unittest.TestCase):
    """Test suite for hardened HTTP client, redirect checks, and 4xx fast fail."""

    def test_client_init_and_headers(self) -> None:
        client = HttpClient(timeout=5.0, user_agent="CustomAgent/1.0")
        self.assertEqual(client.timeout, 5.0)
        self.assertEqual(client.headers["User-Agent"], "CustomAgent/1.0")
        self.assertIn("Accept", client.headers)

    def test_fast_fail_on_404(self) -> None:
        import io
        client = HttpClient()

        start = time.perf_counter()
        with patch.object(client._default_opener, "open") as mock_open:
            import urllib.error

            err = urllib.error.HTTPError(
                url="https://example.com/notfound",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=io.BytesIO(b""),
            )
            mock_open.side_effect = err
            try:
                with self.assertRaises(HttpFastFailError) as ctx:
                    client.get("https://example.com/notfound")
            finally:
                err.close()

        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsInstance(ctx.exception, HttpError)

    def test_fast_fail_on_403(self) -> None:
        import io
        client = HttpClient()
        with patch.object(client._default_opener, "open") as mock_open:
            import urllib.error

            err = urllib.error.HTTPError(
                url="https://example.com/forbidden",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=io.BytesIO(b""),
            )
            mock_open.side_effect = err
            try:
                with self.assertRaises(HttpFastFailError) as ctx:
                    client.fetch_bytes("https://example.com/forbidden")
                self.assertEqual(ctx.exception.status_code, 403)
            finally:
                err.close()

    def test_server_error_500(self) -> None:
        client = HttpClient()
        with patch.object(client._default_opener, "open") as mock_open:
            import urllib.error

            mock_open.side_effect = urllib.error.HTTPError(
                url="https://example.com/error",
                code=500,
                msg="Server Error",
                hdrs={},
                fp=None,
            )
            with self.assertRaises(HttpError) as ctx:
                client.get("https://example.com/error")
            # Should NOT be fast-fail error
            self.assertNotIsInstance(ctx.exception, HttpFastFailError)
            self.assertEqual(ctx.exception.status_code, 500)

    def test_network_error_on_url_error(self) -> None:
        client = HttpClient()
        with patch.object(client._default_opener, "open") as mock_open:
            import urllib.error

            mock_open.side_effect = urllib.error.URLError("Connection refused")
            with self.assertRaises(NetworkError):
                client.fetch_bytes("https://example.com/down")

    def test_fetch_bytes_success(self) -> None:
        client = HttpClient()
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.status = 200
        mock_resp.code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{"status": "ok"}'

        with patch.object(client._default_opener, "open", return_value=mock_resp):
            data = client.fetch_bytes("https://example.com/data")
            self.assertEqual(data, b'{"status": "ok"}')

    def test_redirect_check_detects_spoofed_us_redirect(self) -> None:
        client = HttpClient()
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.status = 301
        mock_resp.code = 301
        mock_resp.headers = {"Location": "https://apps.apple.com/us/app/chatgpt/id6448311069"}
        mock_resp.read.return_value = b"Redirecting"

        with patch.object(client._no_redirect_opener, "open", return_value=mock_resp):
            code, final_url, _, is_spoofed = client.fetch_with_redirect_check(
                url="https://apps.apple.com/xy/app/chatgpt/id6448311069",
                expected_country="xy",
            )
            self.assertEqual(code, 301)
            self.assertEqual(final_url, "https://apps.apple.com/us/app/chatgpt/id6448311069")
            self.assertTrue(is_spoofed)

    def test_redirect_check_valid_country(self) -> None:
        client = HttpClient()
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.status = 200
        mock_resp.code = 200
        mock_resp.headers = {}
        mock_resp.read.return_value = b"<html>Philippines store</html>"

        with patch.object(client._no_redirect_opener, "open", return_value=mock_resp):
            code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
                url="https://apps.apple.com/ph/app/chatgpt/id6448311069",
                expected_country="ph",
            )
            self.assertEqual(code, 200)
            self.assertFalse(is_spoofed)
            self.assertIn(b"Philippines", body)


class TestCacheManager(unittest.TestCase):
    """Test suite for XDG caching, atomic replacement, and TTL validity."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = CacheManager(cache_dir=self.temp_dir.name, default_ttl=3600)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cache_set_and_get(self) -> None:
        self.cache.set("test-key", {"name": "ChatGPT", "count": 42})
        data = self.cache.get("test-key")
        self.assertIsNotNone(data)
        self.assertEqual(data["name"], "ChatGPT")
        self.assertEqual(data["count"], 42)

    def test_atomic_file_creation(self) -> None:
        self.cache.set("atomic-key", [1, 2, 3])
        target_path = self.cache._key_to_path("atomic-key")
        self.assertTrue(target_path.is_file())

        with open(target_path, "r", encoding="utf-8") as f:
            envelope = json.load(f)
        self.assertIn("_metadata", envelope)
        self.assertEqual(envelope["data"], [1, 2, 3])
        self.assertEqual(envelope["_metadata"]["key"], "atomic-key")

    def test_ttl_expiration(self) -> None:
        # Cache with 1 second TTL
        self.cache.set("short-lived", "hello", ttl=1)
        self.assertTrue(self.cache.is_valid("short-lived"))
        self.assertEqual(self.cache.get("short-lived"), "hello")

        # Artificially expire the cache by rewriting expires_at
        target = self.cache._key_to_path("short-lived")
        with open(target, "r", encoding="utf-8") as f:
            env = json.load(f)
        env["_metadata"]["expires_at"] = time.time() - 10.0
        with open(target, "w", encoding="utf-8") as f:
            json.dump(env, f)

        # Standard get returns None (expired)
        self.assertFalse(self.cache.is_valid("short-lived"))
        self.assertIsNone(self.cache.get("short-lived"))

        # ignore_ttl=True allows recovering stale data
        stale = self.cache.get("short-lived", ignore_ttl=True)
        self.assertEqual(stale, "hello")

    def test_corrupt_cache_file_graceful_handling(self) -> None:
        target = self.cache._key_to_path("corrupt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("NOT VALID JSON {{{")

        self.assertIsNone(self.cache.get("corrupt"))
        self.assertFalse(self.cache.is_valid("corrupt"))

    def test_cache_delete_and_clear(self) -> None:
        self.cache.set("k1", 1)
        self.cache.set("k2", 2)
        self.assertTrue(self.cache.has("k1"))
        self.assertTrue(self.cache.delete("k1"))
        self.assertFalse(self.cache.has("k1"))

        cleared = self.cache.clear()
        self.assertEqual(cleared, 1)  # k2 deleted
        self.assertFalse(self.cache.has("k2"))

    def test_in_memory_fallback_on_unwritable_dir(self) -> None:
        # Pass unwritable directory
        with patch.object(Path, "mkdir", side_effect=PermissionError("Permission denied")):
            cm = CacheManager()
            self.assertTrue(cm._in_memory_only)
            cm.set("mem-key", "mem-value")
            self.assertEqual(cm.get("mem-key"), "mem-value")
            self.assertTrue(cm.has("mem-key"))
            self.assertTrue(cm.delete("mem-key"))
            self.assertFalse(cm.has("mem-key"))


class TestCatalogManager(unittest.TestCase):
    """Test suite for catalog discovery, 4-tier fallback, and 5-tier search scoring."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = CacheManager(cache_dir=self.temp_dir.name, default_ttl=3600)
        self.fixture_file = FIXTURES_DIR / "search-index.json"
        with open(self.fixture_file, "r", encoding="utf-8") as f:
            self.fixture_data = json.load(f)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fixture_data_validity(self) -> None:
        self.assertGreaterEqual(len(self.fixture_data), 50)
        slugs = [item["h"] for item in self.fixture_data if "h" in item]
        self.assertIn("/ai-pricing/chatgpt/", slugs)
        self.assertIn("/streaming-pricing/netflix/", slugs)

    def test_remote_fetch_tier_1_success(self) -> None:
        mock_client = MagicMock()
        mock_client.fetch_bytes.return_value = json.dumps(self.fixture_data).encode("utf-8")

        mgr = CatalogManager(cache_manager=self.cache, client=mock_client)
        catalog = mgr.get_catalog(refresh=False)

        self.assertGreaterEqual(len(catalog), 50)
        self.assertTrue(self.cache.is_valid("search-index"))
        mock_client.fetch_bytes.assert_called_once()

    def test_cache_hit_tier_2_avoids_remote_fetch(self) -> None:
        # Prepopulate cache
        self.cache.set("search-index", self.fixture_data)
        mock_client = MagicMock()

        mgr = CatalogManager(cache_manager=self.cache, client=mock_client)
        catalog = mgr.get_catalog(refresh=False)

        self.assertGreaterEqual(len(catalog), 50)
        mock_client.fetch_bytes.assert_not_called()

    def test_fallback_to_bundled_catalog_tier_3(self) -> None:
        mock_client = MagicMock()
        mock_client.fetch_bytes.side_effect = NetworkError("Network down")

        # Empty cache
        mgr = CatalogManager(cache_manager=self.cache, client=mock_client)
        catalog = mgr.get_catalog(refresh=True)

        # Should load bundled default_catalog.json
        self.assertGreaterEqual(len(catalog), 20)
        names = [p.name for p in catalog]
        self.assertIn("ChatGPT", names)
        self.assertIn("Netflix", names)

    def test_fallback_to_stale_cache_tier_4(self) -> None:
        # Put expired data in cache
        self.cache.set("search-index", [{"n": "StaleGPT", "h": "/ai-pricing/stalegpt/", "c": "AI"}], ttl=1)
        target = self.cache._key_to_path("search-index")
        with open(target, "r", encoding="utf-8") as f:
            env = json.load(f)
        env["_metadata"]["expires_at"] = time.time() - 100.0
        with open(target, "w", encoding="utf-8") as f:
            json.dump(env, f)

        mock_client = MagicMock()
        mock_client.fetch_bytes.side_effect = NetworkError("Network down")

        mgr = CatalogManager(cache_manager=self.cache, client=mock_client)
        # Mock bundled fallback failing
        with patch.object(mgr, "_load_bundled_catalog", return_value=None):
            catalog = mgr.get_catalog(refresh=True)
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0].name, "StaleGPT")

    def test_catalog_error_when_all_tiers_fail(self) -> None:
        mock_client = MagicMock()
        mock_client.fetch_bytes.side_effect = NetworkError("Network down")

        mgr = CatalogManager(cache_manager=self.cache, client=mock_client)
        with patch.object(mgr, "_load_bundled_catalog", return_value=None):
            with self.assertRaises(CatalogError):
                mgr.get_catalog(refresh=True)

    def test_5_tier_search_scoring(self) -> None:
        # Prepopulate cache with fixture
        self.cache.set("search-index", self.fixture_data)
        mgr = CatalogManager(cache_manager=self.cache, client=MagicMock())

        # Tier 1: Exact slug match -> score 100
        results_exact_slug = mgr.search("chatgpt", limit=5)
        self.assertTrue(results_exact_slug)
        self.assertEqual(results_exact_slug[0][0].name, "ChatGPT")
        self.assertEqual(results_exact_slug[0][1], 100)

        # Tier 2: Exact name match -> score 90
        results_exact_name = mgr.search("Netflix", limit=5)
        self.assertTrue(results_exact_name)
        self.assertEqual(results_exact_name[0][0].name, "Netflix")
        self.assertEqual(results_exact_name[0][1], 100)  # slug is also netflix -> 100

        # Tier 3: Prefix match -> score 75
        results_prefix = mgr.search("gem", limit=5)
        self.assertTrue(results_prefix)
        self.assertEqual(results_prefix[0][0].name, "Gemini")
        self.assertEqual(results_prefix[0][1], 75)

        # Tier 4: Substring match -> score 60
        results_sub = mgr.search("gpt", limit=5)
        self.assertTrue(results_sub)
        self.assertEqual(results_sub[0][0].name, "ChatGPT")
        self.assertEqual(results_sub[0][1], 60)

        # Tier 5: Fuzzy difflib match -> score 45..55
        results_fuzzy = mgr.search("chtgpt", limit=5)
        self.assertTrue(results_fuzzy)
        self.assertEqual(results_fuzzy[0][0].name, "ChatGPT")
        self.assertGreaterEqual(results_fuzzy[0][1], 45)
        self.assertLessEqual(results_fuzzy[0][1], 55)

        # Non-match -> empty
        results_none = mgr.search("zzznonexistentproductxyz")
        self.assertEqual(results_none, [])

    def test_search_category_filter(self) -> None:
        self.cache.set("search-index", self.fixture_data)
        mgr = CatalogManager(cache_manager=self.cache, client=MagicMock())

        ai_results = mgr.search("ChatGPT", category="AI Subscriptions")
        self.assertTrue(ai_results)
        self.assertEqual(ai_results[0][0].name, "ChatGPT")

        streaming_results = mgr.search("ChatGPT", category="Streaming")
        self.assertEqual(streaming_results, [])

    def test_search_empty_query(self) -> None:
        self.cache.set("search-index", self.fixture_data)
        mgr = CatalogManager(cache_manager=self.cache, client=MagicMock())

        # Empty query without category -> []
        self.assertEqual(mgr.search(""), [])

        # Empty query with category -> products in that category
        streaming = mgr.search("", category="Streaming", limit=5)
        self.assertTrue(streaming)
        for prod, score in streaming:
            self.assertEqual(prod.category, "Streaming")
            self.assertEqual(score, 100)

    def test_resolve_product_exact_slug_and_name(self) -> None:
        self.cache.set("search-index", self.fixture_data)
        mgr = CatalogManager(cache_manager=self.cache, client=MagicMock())

        p1 = mgr.resolve_product("chatgpt")
        self.assertIsNotNone(p1)
        self.assertEqual(p1.name, "ChatGPT")

        p2 = mgr.resolve_product("Netflix")
        self.assertIsNotNone(p2)
        self.assertEqual(p2.slug, "netflix")

    def test_resolve_product_fuzzy_and_missing(self) -> None:
        self.cache.set("search-index", self.fixture_data)
        mgr = CatalogManager(cache_manager=self.cache, client=MagicMock())

        # Typo resolves via fuzzy search pass
        p_typo = mgr.resolve_product("chtgpt")
        self.assertIsNotNone(p_typo)
        self.assertEqual(p_typo.name, "ChatGPT")

        # Unknown returns None
        p_missing = mgr.resolve_product("random-unknown-product-999")
        self.assertIsNone(p_missing)

        # Empty returns None
        self.assertIsNone(mgr.resolve_product(""))


class TestMilestone1HardeningEdgeCases(unittest.TestCase):
    """Direct behavioral tests for Milestone 1 Iteration 2 surgical hardening fixes."""

    def test_client_relative_location_resolved_with_urljoin(self) -> None:
        client = HttpClient()
        with patch.object(client._no_redirect_opener, "open") as mock_open:
            mock_resp = MagicMock()
            mock_resp.status = 302
            mock_resp.headers = {"Location": "/us/app/chatgpt"}
            mock_resp.read.return_value = b""
            mock_open.return_value.__enter__.return_value = mock_resp

            status, final_url, body, is_spoofed = client.fetch_with_redirect_check(
                "https://apps.apple.com/ph/app/chatgpt",
                expected_country="ph",
            )
            self.assertEqual(final_url, "https://apps.apple.com/us/app/chatgpt")
            self.assertTrue(is_spoofed)

    def test_client_http_exception_wrapped_to_network_error(self) -> None:
        import http.client
        client = HttpClient()
        with patch.object(client._default_opener, "open", side_effect=http.client.IncompleteRead(b"abc")):
            with self.assertRaises(NetworkError):
                client.get("https://example.com/test")

        with patch.object(client._no_redirect_opener, "open", side_effect=http.client.BadStatusLine("NOT_HTTP")):
            with self.assertRaises(NetworkError):
                client.check_redirect("https://example.com/test")

        with patch.object(client._no_redirect_opener, "open", side_effect=http.client.HTTPException("Generic error")):
            with self.assertRaises(NetworkError):
                client.fetch_with_redirect_check("https://example.com/test")

    def test_client_module_level_exports(self) -> None:
        import openrank
        import openrank.client
        self.assertTrue(callable(openrank.client.fetch_bytes))
        self.assertTrue(callable(openrank.client.fetch_with_redirect_check))
        self.assertTrue(callable(openrank.fetch_bytes))
        self.assertTrue(callable(openrank.fetch_with_redirect_check))

    def test_cache_non_utf8_binary_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cm = CacheManager(cache_dir=td)
            corrupt_path = cm._key_to_path("corrupt_binary")
            with open(corrupt_path, "wb") as f:
                f.write(b"\xff\xfe\xfd\x80\x00\x01")
            self.assertIsNone(cm.get("corrupt_binary"))
            self.assertFalse(cm.is_valid("corrupt_binary"))

    def test_cache_invalid_expires_at_types_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cm = CacheManager(cache_dir=td)
            for bad_exp in [None, "never", [], {"invalid": True}]:
                key = f"key_{type(bad_exp).__name__}"
                p = cm._key_to_path(key)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"_metadata": {"expires_at": bad_exp}, "data": "safe_val"}, f)
                self.assertEqual(cm.get(key), "safe_val")

    def test_catalog_retains_fresh_remote_data_when_cache_write_fails(self) -> None:
        mock_cache = MagicMock()
        mock_cache.set.side_effect = PermissionError("Disk is read only")
        mock_cache.get.return_value = None

        mock_client = MagicMock()
        fresh_data = [{"n": "Live Remote Service", "h": "/live-service/", "c": "AI"}]
        mock_client.fetch_bytes.return_value = json.dumps(fresh_data).encode("utf-8")

        mgr = CatalogManager(cache_manager=mock_cache, client=mock_client)
        catalog = mgr.get_catalog(refresh=True)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].name, "Live Remote Service")
        self.assertEqual(catalog[0].slug, "live-service")

    def test_price_tier_null_usd_price_defaults_to_zero(self) -> None:
        tier = PriceTier.from_dict({
            "name": "Standard",
            "local_price_str": "Free",
            "usd_price": None,
        })
        self.assertEqual(tier.usd_price, 0.0)
        self.assertEqual(tier.name, "Standard")


if __name__ == "__main__":
    unittest.main()

