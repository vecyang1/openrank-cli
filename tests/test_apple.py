"""Unit and hermetic integration tests for Apple Storefront Validator & Anti-Spoofing."""
from __future__ import annotations

import http.server
import json
import os
import re
import threading
import time
import unittest
from pathlib import Path
from typing import Optional

from openrank.client import (
    HttpClient,
    HttpFastFailError,
    NetworkError,
)
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)
from openrank.pricing import parse_pricing_html
from openrank.storefront_apple import (
    KNOWN_APP_IDS,
    AppleStorefrontValidator,
    cross_validate_pricing,
    parse_apple_iap_html,
    parse_price_and_currency,
    validate_product,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class EphemeralAppleServer:
    """Ephemeral standard library HTTP server for deterministic offline socket tests."""

    def __init__(self, handler_cls) -> None:
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get_url(self, path: str = "") -> str:
        if path and not path.startswith("/"):
            path = "/" + path
        return f"http://127.0.0.1:{self.port}{path}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestAppleIAPParsing(unittest.TestCase):
    """Test suite for parsing Apple In-App Purchases from SSR HTML fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(FIXTURES_DIR / "apple_chatgpt_us.html", "r", encoding="utf-8") as f:
            cls.us_html = f.read()
        with open(FIXTURES_DIR / "apple_chatgpt_ph.html", "r", encoding="utf-8") as f:
            cls.ph_html = f.read()

    def test_us_storefront_iap_extraction(self) -> None:
        """Assert IAP tiers are accurately parsed from Apple US storefront fixture."""
        items = parse_apple_iap_html(self.us_html)
        self.assertEqual(len(items), 3)

        # 1. ChatGPT Plus
        plus = items[0]
        self.assertEqual(plus.name, "ChatGPT Plus")
        self.assertEqual(plus.local_price_str, "$19.99")
        self.assertEqual(plus.numeric_price, 19.99)
        self.assertEqual(plus.currency_symbol, "$")

        # 2. ChatGPT Team
        team = items[1]
        self.assertEqual(team.name, "ChatGPT Team")
        self.assertEqual(team.local_price_str, "$29.99")
        self.assertEqual(team.numeric_price, 29.99)
        self.assertEqual(team.currency_symbol, "$")

        # 3. ChatGPT Pro
        pro = items[2]
        self.assertEqual(pro.name, "ChatGPT Pro")
        self.assertEqual(pro.local_price_str, "$199.99")
        self.assertEqual(pro.numeric_price, 199.99)
        self.assertEqual(pro.currency_symbol, "$")

    def test_ph_storefront_iap_extraction(self) -> None:
        """Assert IAP tiers are accurately parsed from Apple Philippines storefront fixture."""
        items = parse_apple_iap_html(self.ph_html)
        self.assertEqual(len(items), 3)

        # 1. ChatGPT Plus (PHP 999.00)
        plus = items[0]
        self.assertEqual(plus.name, "ChatGPT Plus")
        self.assertEqual(plus.local_price_str, "₱ 999.00")
        self.assertEqual(plus.numeric_price, 999.0)
        self.assertEqual(plus.currency_symbol, "₱")

        # 2. ChatGPT Team (PHP 1,490.00)
        team = items[1]
        self.assertEqual(team.name, "ChatGPT Team")
        self.assertEqual(team.local_price_str, "₱ 1,490.00")
        self.assertEqual(team.numeric_price, 1490.0)
        self.assertEqual(team.currency_symbol, "₱")

        # 3. ChatGPT Pro (PHP 9,990.00)
        pro = items[2]
        self.assertEqual(pro.name, "ChatGPT Pro")
        self.assertEqual(pro.local_price_str, "₱ 9,990.00")
        self.assertEqual(pro.numeric_price, 9990.0)
        self.assertEqual(pro.currency_symbol, "₱")

    def test_fallback_regex_when_script_tag_missing(self) -> None:
        """Assert fallback regex parser extracts identical tiers when script tag is absent."""
        us_no_script = re.sub(r"<script.*?</script>", "", self.us_html, flags=re.DOTALL)
        self.assertNotIn("serialized-server-data", us_no_script)

        items_us = parse_apple_iap_html(us_no_script)
        self.assertEqual(len(items_us), 3)
        self.assertEqual(items_us[0].name, "ChatGPT Plus")
        self.assertEqual(items_us[0].numeric_price, 19.99)
        self.assertEqual(items_us[0].currency_symbol, "$")
        self.assertEqual(items_us[1].name, "ChatGPT Team")
        self.assertEqual(items_us[1].numeric_price, 29.99)
        self.assertEqual(items_us[2].name, "ChatGPT Pro")
        self.assertEqual(items_us[2].numeric_price, 199.99)

        ph_no_script = re.sub(r"<script.*?</script>", "", self.ph_html, flags=re.DOTALL)
        self.assertNotIn("serialized-server-data", ph_no_script)

        items_ph = parse_apple_iap_html(ph_no_script)
        self.assertEqual(len(items_ph), 3)
        self.assertEqual(items_ph[0].name, "ChatGPT Plus")
        self.assertEqual(items_ph[0].numeric_price, 999.0)
        self.assertEqual(items_ph[0].currency_symbol, "₱")
        self.assertEqual(items_ph[1].name, "ChatGPT Team")
        self.assertEqual(items_ph[1].numeric_price, 1490.0)
        self.assertEqual(items_ph[2].name, "ChatGPT Pro")
        self.assertEqual(items_ph[2].numeric_price, 9990.0)

    def test_malformed_json_graceful_fallback(self) -> None:
        """Assert malformed serialized JSON falls back to DOM regex extraction."""
        malformed_html = self.us_html.replace('"$kind": "textPair"', 'INVALID JSON {{{{')
        items = parse_apple_iap_html(malformed_html)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].name, "ChatGPT Plus")

    def test_empty_and_invalid_inputs(self) -> None:
        """Assert empty, None, and non-string inputs return empty list without exception."""
        self.assertEqual(parse_apple_iap_html(""), [])
        self.assertEqual(parse_apple_iap_html("   "), [])
        self.assertEqual(parse_apple_iap_html(None), [])  # type: ignore[arg-type]
        self.assertEqual(parse_apple_iap_html("<html><body>No IAPs here</body></html>"), [])

    def test_app_without_in_app_purchases(self) -> None:
        """Assert app page with no in-app purchases returns empty list."""
        html_no_iap = """
        <html><body>
        <header><h1>Utility App</h1></header>
        <script type="application/json" id="serialized-server-data">
        [{"data": {"content": {"sections": [{"title": "Information", "items": []}]}}}]
        </script>
        </body></html>
        """
        self.assertEqual(parse_apple_iap_html(html_no_iap), [])

    def test_price_and_currency_parser(self) -> None:
        """Assert price and currency parser handles diverse global formats."""
        cases = [
            ("$19.99", 19.99, "$"),
            ("₱ 999.00", 999.0, "₱"),
            ("₱ 1,490.00", 1490.0, "₱"),
            ("CA$24.99/mo", 24.99, "CA$"),
            ("£19.99/month", 19.99, "£"),
            ("€19,99", 19.99, "€"),
            ("19,99 €", 19.99, "€"),
            ("¥14,400", 14400.0, "¥"),
            ("≈ ₱999/mo", 999.0, "₱"),
            ("Free", 0.0, None),
            ("", None, None),
            ("N/A", None, None),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Failed numeric for {raw}")
            self.assertEqual(sym, expected_sym, f"Failed symbol for {raw}")


class TestAppleStorefrontValidator(unittest.TestCase):
    """Test suite for AppleStorefrontValidator URL routing, resolution, and models."""

    def setUp(self) -> None:
        self.validator = AppleStorefrontValidator()

    def test_known_app_ids_presence_and_format(self) -> None:
        """Assert KNOWN_APP_IDS dictionary maps primary products to valid Apple App IDs."""
        expected = ["chatgpt", "claude", "youtube", "netflix", "spotify"]
        for slug in expected:
            self.assertIn(slug, self.validator.KNOWN_APP_IDS)
            app_id = self.validator.KNOWN_APP_IDS[slug]
            self.assertTrue(app_id.startswith("id"), f"App ID {app_id} should start with 'id'")
            self.assertTrue(app_id[2:].isdigit(), f"App ID {app_id} should have digits after 'id'")

    def test_unknown_product_slug_fast_rejection(self) -> None:
        """Assert unknown product slug returns invalid result without issuing network requests."""
        res = self.validator.validate_product("completely-unknown-slug-xyz", "us")
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_spoofed)
        self.assertIn("Unknown product slug", res.error or "")

    def test_embedded_app_id_slug_resolution(self) -> None:
        """Assert product slug with embedded App ID (e.g. custom-app/id123456789) resolves cleanly."""
        class MockClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 200, url, b"", False

        v = AppleStorefrontValidator(client=MockClient())
        res = v.validate_product("custom-app/id987654321", "us")
        self.assertEqual(res.app_id, "id987654321")
        self.assertEqual(res.requested_url, "https://apps.apple.com/us/app/id987654321/id987654321")

    def test_validation_result_model_invariants(self) -> None:
        """Assert AppleValidationResult model enforces consistency across alias attributes."""
        res = AppleValidationResult(
            product_slug="chatgpt",
            country_code="US",
            requested_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            final_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            status_code=200,
            items=[AppleIAPItem(name="ChatGPT Plus", local_price_str="$19.99", numeric_price=19.99, currency_symbol="$")],
        )
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_spoofed)
        self.assertEqual(res.items, res.iaps)
        self.assertEqual(res.final_url, res.redirect_target)
        d = res.to_dict()
        self.assertEqual(d["product_slug"], "chatgpt")
        self.assertEqual(len(d["items"]), 1)


class TestAntiSpoofingRedirectDetection(unittest.TestCase):
    """Test suite for detecting Apple CDN 301/302 fallback redirect spoofing."""

    def test_anti_spoofing_detects_301_to_us_store(self) -> None:
        """Assert requesting invalid country 'xy' redirected to /us/ flags is_spoofed=True."""
        class SpoofedRedirectClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                # Simulates Apple CDN 301 fallback redirect to US store
                return 301, "https://apps.apple.com/us/app/chatgpt/id6448311069", b"", True

        validator = AppleStorefrontValidator(client=SpoofedRedirectClient())
        res = validator.validate_product("chatgpt", "xy")

        self.assertTrue(res.is_spoofed)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.status_code, 301)
        self.assertIn("anti-spoofing triggered", res.error or "")
        self.assertEqual(res.items, [])

    def test_in_country_redirect_not_spoofed(self) -> None:
        """Assert legitimate in-country canonical redirect (staying in /ph/) is not flagged as spoofed."""
        with open(FIXTURES_DIR / "apple_chatgpt_ph.html", "rb") as f:
            ph_body = f.read()

        class InCountryRedirectClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 200, "https://apps.apple.com/ph/app/chatgpt/id6448311069", ph_body, False

        validator = AppleStorefrontValidator(client=InCountryRedirectClient())
        res = validator.validate_product("chatgpt", "ph")

        self.assertFalse(res.is_spoofed)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.items), 3)

    def test_us_store_redirect_to_us_not_spoofed(self) -> None:
        """Assert requesting country 'us' redirected to /us/ is legitimate and not flagged as spoofed."""
        with open(FIXTURES_DIR / "apple_chatgpt_us.html", "rb") as f:
            us_body = f.read()

        class USRedirectClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 200, "https://apps.apple.com/us/app/chatgpt/id6448311069", us_body, False

        validator = AppleStorefrontValidator(client=USRedirectClient())
        res = validator.validate_product("chatgpt", "us")

        self.assertFalse(res.is_spoofed)
        self.assertTrue(res.is_valid)


class TestDeterministicFastFail(unittest.TestCase):
    """Test suite for immediate deterministic fast-fail (<0.5s) on 4xx client errors."""

    def test_404_fast_fail_terminates_immediately(self) -> None:
        """Assert 404 client error raises HttpFastFailError and completes in <0.05s."""
        class FastFail404Client:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise HttpFastFailError(
                    status_code=404,
                    message=f"HTTP client error 404 for URL: {url}",
                    url=url,
                )

        validator = AppleStorefrontValidator(client=FastFail404Client())
        start = time.perf_counter()
        res = validator.validate_product("chatgpt", "cn")
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5, "404 fast-fail exceeded 0.5s SLA")
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_spoofed)
        self.assertEqual(res.status_code, 404)
        self.assertIn("App not available in storefront", res.error or "")

    def test_403_forbidden_fast_fail(self) -> None:
        """Assert 403 Forbidden raises HttpFastFailError immediately."""
        class FastFail403Client:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise HttpFastFailError(
                    status_code=403,
                    message=f"HTTP client error 403 for URL: {url}",
                    url=url,
                )

        validator = AppleStorefrontValidator(client=FastFail403Client())
        res = validator.validate_product("chatgpt", "ir")
        self.assertFalse(res.is_valid)
        self.assertEqual(res.status_code, 403)

    def test_network_error_handled_gracefully(self) -> None:
        """Assert NetworkError (timeout, connection reset) is caught without uncaught exception."""
        class NetworkFailingClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise NetworkError("Connection refused by peer")

        validator = AppleStorefrontValidator(client=NetworkFailingClient())
        res = validator.validate_product("chatgpt", "us")
        self.assertFalse(res.is_valid)
        self.assertIn("Network error", res.error or "")


class TestParityCrossValidation(unittest.TestCase):
    """Test suite for cross-validating OpenTheRank extracted pricing against Apple IAP parity."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(FIXTURES_DIR / "chatgpt_pricing.html", "r", encoding="utf-8") as f:
            cls.otr_html = f.read()
        with open(FIXTURES_DIR / "apple_chatgpt_ph.html", "rb") as f:
            cls.apple_ph_bytes = f.read()
        with open(FIXTURES_DIR / "apple_chatgpt_us.html", "rb") as f:
            cls.apple_us_bytes = f.read()

        cls.product = ProductSummary(
            name="ChatGPT",
            slug="chatgpt",
            path="/ai-pricing/chatgpt/",
            category="AI Subscriptions",
            url="https://opentherank.com/ai-pricing/chatgpt/",
        )

    def _get_mock_validator(self) -> AppleStorefrontValidator:
        test_self = self

        class MockParityClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                exp = (expected_country or "").lower()
                if exp == "ph":
                    return 200, url, test_self.apple_ph_bytes, False
                elif exp == "us":
                    return 200, url, test_self.apple_us_bytes, False
                elif exp == "xy":
                    return 301, "https://apps.apple.com/us/app/chatgpt/id6448311069", b"", True
                elif exp == "cn":
                    raise HttpFastFailError(404, "Not Found", url=url)
                elif exp == "zz":
                    return 200, url, test_self.apple_ph_bytes, False
                return 404, url, b"", False

        return AppleStorefrontValidator(client=MockParityClient())

    def test_philippines_parity_verified(self) -> None:
        """Assert cross-validation succeeds for Philippines storefront (Plus: ₱ 999.00)."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        validator = self._get_mock_validator()

        is_valid, err = validator.cross_validate_pricing(otr_res, "PH")
        self.assertTrue(is_valid)
        self.assertIsNone(err)
        self.assertTrue(otr_res.first_party_validated)
        self.assertEqual(otr_res.validation_source, "apple")
        self.assertTrue(any("Parity verified" in n for n in otr_res.validation_notes))

    def test_us_baseline_parity_verified(self) -> None:
        """Assert cross-validation succeeds for US storefront (Plus: $19.99)."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        validator = self._get_mock_validator()

        is_valid, err = validator.cross_validate_pricing(otr_res, "US")
        self.assertTrue(is_valid)
        self.assertIsNone(err)
        self.assertTrue(otr_res.first_party_validated)

    def test_price_mismatch_detected(self) -> None:
        """Assert price mismatch between OpenTheRank and Apple IAP fails validation."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        # Artificially alter Philippines price in OTR result to ₱1,500
        for r in otr_res.records:
            if r.country_code.upper() == "PH":
                r.primary_tier.local_price_str = "≈ ₱1,500/mo"

        validator = self._get_mock_validator()
        is_valid, err = validator.cross_validate_pricing(otr_res, "PH")

        self.assertFalse(is_valid)
        self.assertIsNotNone(err)
        self.assertIn("Price parity mismatch", err or "")
        self.assertFalse(otr_res.first_party_validated)

    def test_currency_mismatch_detected(self) -> None:
        """Assert currency mismatch between OpenTheRank and Apple IAP fails validation."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        # Artificially alter Philippines currency to $
        for r in otr_res.records:
            if r.country_code.upper() == "PH":
                r.primary_tier.local_price_str = "≈ $999/mo"

        validator = self._get_mock_validator()
        is_valid, err = validator.cross_validate_pricing(otr_res, "PH")

        self.assertFalse(is_valid)
        self.assertIsNotNone(err)
        self.assertIn("Currency mismatch", err or "")

    def test_spoofed_storefront_rejected_in_cross_validation(self) -> None:
        """Assert spoofed redirect storefront fails cross-validation with anti-spoofing alert."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        validator = self._get_mock_validator()

        is_valid, err = validator.cross_validate_pricing(otr_res, "XY")
        self.assertFalse(is_valid)
        self.assertIn("Anti-spoofing alert", err or "")

    def test_missing_country_record_rejected(self) -> None:
        """Assert cross-validation for a country not present in OpenTheRank result fails."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        validator = self._get_mock_validator()

        # ZZ is not in OTR ChatGPT pricing
        is_valid, err = validator.cross_validate_pricing(otr_res, "ZZ")
        self.assertFalse(is_valid)
        self.assertIn("No pricing record found", err or "")

    def test_module_level_cross_validate_helper(self) -> None:
        """Assert module-level cross_validate_pricing function works identically."""
        otr_res = parse_pricing_html(self.otr_html, self.product)
        # Unknown country without network will fail cleanly
        is_valid, err = cross_validate_pricing(otr_res, "ZZ")
        self.assertFalse(is_valid)


class TestLiveEphemeralAntiSpoofingAndFastFail(unittest.TestCase):
    """End-to-end socket tests using EphemeralHttpServer to verify real HTTP behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(FIXTURES_DIR / "apple_chatgpt_ph.html", "rb") as f:
            cls.ph_body = f.read()

        ph_data = cls.ph_body

        class AppleMockHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                # 1. Non-existent storefront -> 404 Not Found
                if "/cn/" in self.path or "/notfound/" in self.path:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html>404 Not Found</html>")
                # 2. Unsupported storefront -> 301 Redirect to /us/
                elif "/xy/" in self.path or "/spoof/" in self.path:
                    self.send_response(301)
                    self.send_header("Location", "/us/app/chatgpt/id6448311069")
                    self.end_headers()
                # 3. Legitimate PH storefront -> 200 OK
                elif "/ph/" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(ph_data)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def log_message(self, format, *args):
                pass  # Suppress stderr logging during test runs

        cls.server = EphemeralAppleServer(AppleMockHandler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_live_404_fast_fail_latency_benchmark(self) -> None:
        """Assert real HTTP 404 fast-fail executes in <0.5s without retry latency."""
        url = self.server.get_url("/cn/app/chatgpt/id6448311069")
        start = time.perf_counter()
        with self.assertRaises(HttpFastFailError) as ctx:
            self.client.fetch_with_redirect_check(url, expected_country="cn")
        elapsed = time.perf_counter() - start

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertLess(elapsed, 0.5, f"404 fast-fail took {elapsed:.4f}s, exceeding 0.5s threshold")

    def test_live_anti_spoofing_redirect_detection(self) -> None:
        """Assert real HTTP 301 to /us/ is intercepted and flagged is_spoofed=True."""
        url = self.server.get_url("/xy/app/chatgpt/id6448311069")
        status, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="xy"
        )
        self.assertEqual(status, 301)
        self.assertTrue(is_spoofed)
        self.assertIn("/us/", final_url)

    def test_live_philippines_storefront_200_extraction(self) -> None:
        """Assert real HTTP 200 response parses all 3 IAPs cleanly over live loopback socket."""
        url = self.server.get_url("/ph/app/chatgpt/id6448311069")
        status, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(status, 200)
        self.assertFalse(is_spoofed)

        items = parse_apple_iap_html(body.decode("utf-8"))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].name, "ChatGPT Plus")
        self.assertEqual(items[0].numeric_price, 999.0)


class TestMilestone3RemediationRegressions(unittest.TestCase):
    """Regression test suite verifying all 7 Milestone 3 empirical defect remediations."""

    def test_sub_dollar_pricing_regex_variations(self) -> None:
        """Verify sub-dollar prices (.99, $.99, .49, ,99 €) parse accurately."""
        cases = [
            ("$.99", 0.99, "$"),
            (".99", 0.99, None),
            ("$0.99", 0.99, "$"),
            (".49", 0.49, None),
            (",99 €", 0.99, "€"),
            ("CA$.99", 0.99, "CA$"),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Failed for '{raw}'")
            self.assertEqual(sym, expected_sym, f"Failed symbol for '{raw}'")

    def test_promotional_free_and_ad_free_strings(self) -> None:
        """Verify promotional free-trial and ad-free strings preserve real prices."""
        cases = [
            ("Free trial then $19.99", 19.99, "$"),
            ("Try free for 7 days, then $9.99/mo", 9.99, "$"),
            ("7-day free trial, then $4.99/month", 4.99, "$"),
            ("Ad-free $9.99", 9.99, "$"),
            ("Ad free $9.99", 9.99, "$"),
            ("Freemium $14.99", 14.99, "$"),
            ("Toll-free customer care + $49.99", 49.99, "$"),
            ("Risk-free trial, then $19.99", 19.99, "$"),
            ("Free", 0.0, None),
            ("FREE", 0.0, None),
            ("$0.00", 0.0, "$"),
            ("0 €", 0.0, "€"),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Failed for '{raw}'")
            self.assertEqual(sym, expected_sym, f"Failed symbol for '{raw}'")

    def test_unicode_non_breaking_spaces_and_thin_spaces(self) -> None:
        """Verify French non-breaking space (U+00A0) and Russian thin space (U+2009) parse accurately."""
        self.assertEqual(parse_price_and_currency("1\u00a0490,00 €"), (1490.0, "€"))
        self.assertEqual(parse_price_and_currency("14\u2009400 ₽"), (14400.0, "₽"))
        self.assertEqual(parse_price_and_currency("1\u202f490,00 €"), (1490.0, "€"))

    def test_gemini_known_app_id_mapping(self) -> None:
        """Verify gemini and google-gemini are mapped to id6477507566 in KNOWN_APP_IDS."""
        self.assertIn("gemini", KNOWN_APP_IDS)
        self.assertIn("google-gemini", KNOWN_APP_IDS)
        self.assertEqual(KNOWN_APP_IDS["gemini"], "id6477507566")
        self.assertEqual(KNOWN_APP_IDS["google-gemini"], "id6477507566")

        v = AppleStorefrontValidator()
        res = v.validate_product("gemini", "us")
        self.assertEqual(res.app_id, "id6477507566")

    def test_two_pass_tier_matching_shuffled_order(self) -> None:
        """Verify two-pass matching pairs ChatGPT Team with Team even when Plus is listed first."""
        product = ProductSummary("ChatGPT", "chatgpt", "/chatgpt", "AI", "https://opentherank.com/chatgpt")
        record = RegionPricingRecord(
            country_code="US",
            region_name="United States",
            platform="appstore",
            primary_tier=PriceTier("ChatGPT Team", "$29.99", 29.99),
            additional_tiers={"plus": PriceTier("ChatGPT Plus", "$19.99", 19.99)},
            vs_us_delta=0.0,
            tax_info="",
            risk_level="Low",
            risk_note="",
            snapshot_date="",
        )
        otr_res = ProductPricingResult(product=product, records=[record])

        class MultiTierClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html_multi = """
                <script id="serialized-server-data">
                [{"data": {"content": {"sections": [{"title": "In-App Purchases", "items": [
                    {"leadingText": "ChatGPT Plus", "trailingText": "$19.99"},
                    {"leadingText": "ChatGPT Team", "trailingText": "$29.99"},
                    {"leadingText": "ChatGPT Pro", "trailingText": "$199.99"}
                ]}]}}}]
                </script>
                """
                return 200, url, html_multi.encode("utf-8"), False

        validator = AppleStorefrontValidator(client=MultiTierClient())
        is_valid, err = validator.cross_validate_pricing(otr_res, "US")
        self.assertTrue(is_valid, f"Cross validation should pass: {err}")
        self.assertIsNone(err)
        self.assertTrue(any("ChatGPT Team" in n for n in otr_res.validation_notes))

    def test_follow_in_country_301_canonical_redirect(self) -> None:
        """Verify in-country canonical 301 is followed and items are populated."""
        class RedirectClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 301, "https://apps.apple.com/ph/app/chatgpt-official/id6448311069", b"301 Moved", False

            def get(self, url, follow_redirects=True):
                html = """
                <script id="serialized-server-data">
                [{"data": {"content": {"sections": [{"title": "In-App Purchases", "items": [
                    {"leadingText": "ChatGPT Plus", "trailingText": "₱ 999.00"}
                ]}]}}}]
                </script>
                """
                return 200, {}, html.encode("utf-8")

        validator = AppleStorefrontValidator(client=RedirectClient())
        res = validator.validate_product("chatgpt", "ph")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_spoofed)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0].name, "ChatGPT Plus")
        self.assertEqual(res.items[0].numeric_price, 999.0)

    def test_scoped_li_boundary_fallback_regex(self) -> None:
        """Verify HTML fallback regex does not bleed price into title across li elements."""
        html_bleed = """
        <section class="in-app-purchases-section">
        <ol class="list--in-app-purchases">
          <li class="list-item">
            <span class="list-item__title">Title Without Price</span>
          </li>
          <li class="list-item">
            <span class="list-item__title">Valid Subscription</span>
            <span class="list-item__price">$14.99</span>
          </li>
        </ol>
        </section>
        """
        items = parse_apple_iap_html(html_bleed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "Valid Subscription")
        self.assertEqual(items[0].numeric_price, 14.99)
        self.assertEqual(items[0].currency_symbol, "$")


if __name__ == "__main__":
    unittest.main()
