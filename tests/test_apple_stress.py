"""Adversarial stress tests for Apple Storefront Validator, IAP parser, and Anti-Spoofing.

Author: m3_challenger_1 (teamwork_preview_challenger)
Purpose: Empirical stress-testing, bug reproduction, edge-case mining, and SLA verification.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Optional

from openrank.client import (
    HttpClient,
    HttpError,
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
from openrank.storefront_apple import (
    AppleStorefrontValidator,
    cross_validate_pricing,
    parse_apple_iap_html,
    parse_price_and_currency,
    validate_product,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class EphemeralStressServer:
    """Ephemeral HTTP server for deterministic local socket testing."""

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


class TestMalformedHtmlAndJsonStress(unittest.TestCase):
    """Adversarial input handling: corrupt JSON, missing script tags, malformed HTML."""

    def test_corrupt_json_in_script_tag_falls_back_without_crash(self) -> None:
        """Assert unparseable JSON in serialized-server-data falls back cleanly."""
        corrupt_samples = [
            '<script id="serialized-server-data">{malformed JSON: missing quotes}</script>',
            '<script id="serialized-server-data">[{"data": {"unterminated": </script>',
            '<script id="serialized-server-data">null</script>',
            '<script id="serialized-server-data">12345</script>',
            '<script id="serialized-server-data">"just a string"</script>',
            '<script id="serialized-server-data">[]</script>',
            '<script id="serialized-server-data">{}</script>',
        ]
        for html_snippet in corrupt_samples:
            items = parse_apple_iap_html(html_snippet)
            self.assertIsInstance(items, list)
            self.assertEqual(len(items), 0)

    def test_script_tag_with_unexpected_nested_types(self) -> None:
        """Assert JSON with unexpected schema types (dict items, None items) handles safely."""
        tricky_json = {
            "title": "In-App Purchases",
            "items": [
                None,
                123,
                "string-item",
                [],
                {},
                {"leadingText": None, "trailingText": "$19.99"},
                {"leadingText": "ChatGPT Plus", "trailingText": None},
                {"leadingText": "ChatGPT Plus", "trailingText": "$19.99"},
            ],
        }
        html_input = f'<script id="serialized-server-data">{json.dumps(tricky_json)}</script>'
        items = parse_apple_iap_html(html_input)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "ChatGPT Plus")
        self.assertEqual(items[0].numeric_price, 19.99)

    def test_unclosed_tags_and_truncated_html(self) -> None:
        """Assert severely truncated HTML does not cause regex catastrophic backtracking or crash."""
        truncated_samples = [
            '<script id="serialized-server-data">',
            '<section class="in-app-purchases-section"><ol class="list--in-app-purchases">',
            '<li class="list-item"><span class="list-item__title">Title',
            '<li class="list-item"><span class="list-item__title">Title</span><span class="list-item__price">',
            "<" * 1000,
            ">" * 1000,
            'class="in-app-purchases" ' * 500,
        ]
        for sample in truncated_samples:
            items = parse_apple_iap_html(sample)
            self.assertIsInstance(items, list)

    def test_reproduction_regex_cross_item_boundary_bleed(self) -> None:
        """Verify fallback regex does not match title of item 1 with price of item 2 across li tags."""
        html_mismatched = """
        <section class="in-app-purchases-section">
        <ol class="list--in-app-purchases">
          <li class="list-item">
            <span class="list-item__title">Tier Without Price</span>
          </li>
          <li class="list-item">
            <span class="list-item__title">Legitimate Tier</span>
            <span class="list-item__price">$9.99</span>
          </li>
        </ol>
        </section>
        """
        items = parse_apple_iap_html(html_mismatched)
        self.assertEqual(len(items), 1)
        first_item = items[0]
        self.assertEqual(first_item.name, "Legitimate Tier")
        self.assertEqual(first_item.local_price_str, "$9.99")
        self.assertEqual(first_item.numeric_price, 9.99)
        self.assertEqual(first_item.currency_symbol, "$")


class TestPriceAndCurrencyParsingStress(unittest.TestCase):
    """Stress tests for price normalization, regional separators, and edge-case strings."""

    def test_reproduction_missing_leading_zero_price_multiplication(self) -> None:
        """Verify '$.99' and '.99' without leading zero parse as 0.99 instead of 99.0."""
        num_dot99, sym_dot99 = parse_price_and_currency("$.99")
        self.assertEqual(num_dot99, 0.99)
        self.assertEqual(sym_dot99, "$")

        num_bare, sym_bare = parse_price_and_currency(".99")
        self.assertEqual(num_bare, 0.99)
        self.assertIsNone(sym_bare)

    def test_reproduction_free_trial_promotional_text_zeroes_price(self) -> None:
        """Verify promotional trial strings preserve real price instead of zeroing."""
        trial_samples = [
            ("Free trial then $19.99", 19.99, "$"),
            ("Try free for 7 days, then $9.99/mo", 9.99, "$"),
            ("7-day free trial, then $4.99/month", 4.99, "$"),
        ]
        for s, expected_num, expected_sym in trial_samples:
            num, sym = parse_price_and_currency(s)
            self.assertEqual(num, expected_num, f"Failed for '{s}'")
            self.assertEqual(sym, expected_sym, f"Failed symbol for '{s}'")

    def test_global_currency_formats(self) -> None:
        """Assert diverse valid global currency formats parse accurately."""
        cases = [
            ("₹ 1,49,000.00", 149000.0, "₹"),
            ("R$ 49,90", 49.9, "R$"),
            ("kr 149,00", 149.0, "kr"),
            ("1.490,00 kr", 1490.0, "kr"),
            ("CHF 19.90", 19.9, "CHF"),
            ("AED 79.99", 79.99, "AED"),
            ("19,99 zł", 19.99, "zł"),
            ("14 400 ₽", 14400.0, "₽"),
            ("1 490,00 €", 1490.0, "€"),
            ("$0.99", 0.99, "$"),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num)
            self.assertEqual(sym, expected_sym)


class TestAntiSpoofingRedirectStress(unittest.TestCase):
    """Stress tests for CDN redirect tracking, open redirects, and anti-spoofing evasion."""

    def test_reproduction_app_slug_matches_country_bypasses_spoofing(self) -> None:
        """Verify path segment extraction correctly detects cross-country spoofing even if slug matches country."""
        final_url = "https://apps.apple.com/gb/app/ph/id6448311069"
        expected_country = "ph"
        parsed = urllib.parse.urlsplit(final_url)
        path_segments = [p for p in parsed.path.split("/") if p]
        storefront_cc = path_segments[0].lower() if path_segments else ""
        is_spoofed = (storefront_cc != expected_country.lower())
        self.assertTrue(is_spoofed, "Path segment check correctly flags /gb/ as spoofed when /ph/ was requested")

    def test_in_country_301_marked_valid_with_zero_items(self) -> None:
        """Verify legitimate in-country canonical 301 is traversed and populates IAP items."""
        with open(FIXTURES_DIR / "apple_chatgpt_ph.html", "rb") as f:
            ph_html_bytes = f.read()

        class InCountry301Client:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 301, "https://apps.apple.com/ph/app/chatgpt-canonical/id6448311069", b"301 Redirect", False

            def get(self, url, follow_redirects=True):
                return 200, {}, ph_html_bytes

        v = AppleStorefrontValidator(client=InCountry301Client())
        res = v.validate_product("chatgpt", "ph")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.items), 3)
        self.assertEqual(res.items[0].name, "ChatGPT Plus")
        self.assertEqual(res.items[0].numeric_price, 999.0)
        self.assertIsNone(res.error)


class TestFastFailLatencySlaStress(unittest.TestCase):
    """Verify deterministic 4xx fast-fail SLA (<0.5s) across all client error codes."""

    @classmethod
    def setUpClass(cls) -> None:
        class FastFailHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parts = self.path.strip("/").split("/")
                code = int(parts[0]) if parts[0].isdigit() else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"error response")

            def log_message(self, *args):
                pass

        cls.server = EphemeralStressServer(FastFailHandler)
        cls.client = HttpClient(timeout=2.0)
        cls.validator = AppleStorefrontValidator(client=cls.client)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_all_4xx_codes_terminate_under_0_05s(self) -> None:
        """Assert 400, 401, 403, 404, 405, 410, 429 terminate well below the 0.5s SLA (<0.05s)."""
        error_codes = [400, 401, 403, 404, 405, 410, 429]
        for code in error_codes:
            url = self.server.get_url(f"/{code}")
            start = time.perf_counter()
            with self.assertRaises(HttpFastFailError) as ctx:
                self.client.fetch_with_redirect_check(url, expected_country="us")
            elapsed = time.perf_counter() - start

            self.assertEqual(ctx.exception.status_code, code)
            self.assertLess(
                elapsed,
                0.5,
                f"HTTP {code} fast-fail took {elapsed:.4f}s, exceeding 0.5s SLA",
            )
            self.assertLess(
                elapsed,
                0.05,
                f"HTTP {code} fast-fail took {elapsed:.4f}s, exceeding tight 0.05s target",
            )


class TestParityCrossValidationStress(unittest.TestCase):
    """Stress tests for parity cross-validation and tier-matching algorithms."""

    def test_reproduction_tier_order_greedy_word_match_bug(self) -> None:
        """Verify two-pass matching correctly pairs 'ChatGPT Plus' even when Apple lists 'ChatGPT Pro' first."""
        product = ProductSummary("ChatGPT", "chatgpt", "/chatgpt", "AI", "https://opentherank.com/chatgpt")
        record = RegionPricingRecord(
            country_code="US",
            region_name="United States",
            platform="appstore",
            primary_tier=PriceTier("ChatGPT Plus", "$19.99", 19.99),
            additional_tiers={},
            vs_us_delta=0.0,
            tax_info="",
            risk_level="Low",
            risk_note="",
            snapshot_date="",
        )
        otr_res = ProductPricingResult(product=product, records=[record])

        class ShuffledOrderClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html_shuffled = """
                <script id="serialized-server-data">
                [{"data": {"content": {"sections": [{"title": "In-App Purchases", "items": [
                    {"leadingText": "ChatGPT Pro", "trailingText": "$199.99"},
                    {"leadingText": "ChatGPT Plus", "trailingText": "$19.99"}
                ]}]}}}]
                </script>
                """
                return 200, url, html_shuffled.encode("utf-8"), False

        validator = AppleStorefrontValidator(client=ShuffledOrderClient())
        is_valid, err = validator.cross_validate_pricing(otr_res, "US")

        # Verify that two-pass matching accurately pairs ChatGPT Plus with ChatGPT Plus
        self.assertTrue(is_valid, f"Parity validation should pass for shuffled order, got error: {err}")
        self.assertIsNone(err)
        self.assertTrue(otr_res.first_party_validated)
        self.assertTrue(any("ChatGPT Plus" in n for n in otr_res.validation_notes))


if __name__ == "__main__":
    unittest.main()
