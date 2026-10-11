"""Empirical stress tests and latency benchmark for 4xx fast-fail and currency extraction.

Author: m3_challenger_2 (teamwork_preview_challenger)
Purpose: Independent empirical verification, latency SLA benchmarking (<0.5s),
         and stress-testing currency extraction across obscure global formats,
         Unicode symbols, zero-decimal currencies, free tiers, and malformed inputs.
"""
from __future__ import annotations

import html
import http.server
import json
import statistics
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

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
from openrank.pricing import (
    PricingEngine,
    _parse_usd_price,
    parse_pricing_html,
)
from openrank.storefront_apple import (
    AppleStorefrontValidator,
    parse_apple_iap_html,
    parse_price_and_currency,
    validate_product,
)


class EphemeralFastFailServer:
    """Ephemeral HTTP server for deterministic local socket benchmark tests."""

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


class TestDeterministic4xxFastFailBenchmark(unittest.TestCase):
    """Empirical latency benchmarking and deterministic fast-fail assertions for HTTP 4xx."""

    @classmethod
    def setUpClass(cls) -> None:
        class FastFailBenchmarkHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path.strip("/")
                if p.startswith("status/"):
                    code = int(p.split("/")[1])
                    self.send_response(code)
                    if code == 429:
                        self.send_header("Retry-After", "120")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(f'{{"error": "client_error", "code": {code}}}'.encode("utf-8"))
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def log_message(self, *args):
                pass

        cls.server = EphemeralFastFailServer(FastFailBenchmarkHandler)
        cls.client = HttpClient(timeout=5.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_benchmark_4xx_latency_sla(self) -> None:
        """Empirically benchmark status codes 400, 401, 403, 404, 429 across 30 iterations each.

        Asserts:
        - Latency is strictly < 0.5s for every individual request (P100 < 0.5s).
        - Exception raised is strictly HttpFastFailError (not generic HttpError or NetworkError).
        - Status code is preserved accurately on the exception.
        - On 429, Retry-After header is received and NO 120-second sleep occurs.
        """
        target_codes = [400, 401, 403, 404, 429]
        iterations = 30
        latency_stats: Dict[int, Dict[str, float]] = {}

        for code in target_codes:
            url = self.server.get_url(f"/status/{code}")
            latencies: List[float] = []

            for _ in range(iterations):
                start = time.perf_counter()
                with self.assertRaises(HttpFastFailError) as ctx:
                    self.client.get(url)
                elapsed = time.perf_counter() - start
                latencies.append(elapsed)

                self.assertEqual(ctx.exception.status_code, code)
                self.assertEqual(ctx.exception.url, url)
                if code == 429:
                    self.assertEqual(ctx.exception.headers.get("Retry-After"), "120")

            latencies.sort()
            min_l = latencies[0]
            max_l = latencies[-1]
            mean_l = statistics.mean(latencies)
            median_l = statistics.median(latencies)
            p99_l = latencies[int(len(latencies) * 0.99)]

            latency_stats[code] = {
                "min": min_l,
                "max": max_l,
                "mean": mean_l,
                "median": median_l,
                "p99": p99_l,
            }

            # SLA Check: Strict < 0.5s
            self.assertLess(
                max_l,
                0.5,
                f"HTTP {code} maximum latency {max_l:.4f}s exceeded 0.5s SLA!",
            )
            # Tight localhost assertion: should be well under 0.05s
            self.assertLess(
                max_l,
                0.05,
                f"HTTP {code} maximum latency {max_l:.4f}s exceeded 0.05s tight localhost bound",
            )

        # Store stats for test verification and handoff reporting
        self.__class__.benchmark_metrics = latency_stats

    def test_429_retry_after_120_immediate_termination(self) -> None:
        """Assert 429 with Retry-After: 120 fails deterministically in < 0.05s without sleeping."""
        url = self.server.get_url("/status/429")
        start = time.perf_counter()
        with self.assertRaises(HttpFastFailError) as ctx:
            self.client.fetch_bytes(url)
        elapsed = time.perf_counter() - start

        self.assertLess(
            elapsed,
            0.5,
            f"429 Retry-After took {elapsed:.4f}s, violating 0.5s SLA",
        )
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "120")

    def test_fast_fail_across_all_client_interfaces(self) -> None:
        """Assert fast-fail behaves consistently across get, fetch_bytes, check_redirect, fetch_with_redirect_check."""
        url_404 = self.server.get_url("/status/404")
        url_403 = self.server.get_url("/status/403")

        # 1. get()
        t0 = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            self.client.get(url_404)
        self.assertLess(time.perf_counter() - t0, 0.5)

        # 2. fetch_bytes()
        t0 = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            self.client.fetch_bytes(url_404)
        self.assertLess(time.perf_counter() - t0, 0.5)

        # 3. check_redirect()
        t0 = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            self.client.check_redirect(url_403)
        self.assertLess(time.perf_counter() - t0, 0.5)

        # 4. fetch_with_redirect_check()
        t0 = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            self.client.fetch_with_redirect_check(url_404, expected_country="us")
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_validator_handles_4xx_without_raising_to_caller(self) -> None:
        """Assert AppleStorefrontValidator catches HttpFastFailError and converts it to a clean result."""
        class Mock404Client:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise HttpFastFailError(404, "Not Found", url=url)

        validator = AppleStorefrontValidator(client=Mock404Client())
        start = time.perf_counter()
        res = validator.validate_product("chatgpt", "us")
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.status_code, 404)
        self.assertIn("404", res.error or "")


class TestCurrencyExtractionGlobalStress(unittest.TestCase):
    """Stress tests for parse_price_and_currency across global and extreme currency formats."""

    def test_zero_decimal_currencies(self) -> None:
        """Stress-test zero-decimal currencies (JPY, KRW, VND, CLP, IDR, PYG)."""
        cases = [
            ("¥1,500", 1500.0, "¥"),
            ("JP¥ 1,500", 1500.0, "JP¥"),
            ("¥1500", 1500.0, "¥"),
            ("1500円", 1500.0, "円"),
            ("₩22,000", 22000.0, "₩"),
            ("KRW 22,000", 22000.0, "KRW"),
            ("22,000원", 22000.0, "원"),
            ("₩ 14,000", 14000.0, "₩"),
            ("500,000 VND", 500000.0, "VND"),
            ("Rp 150,000", 150000.0, "Rp"),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Failed for zero-decimal: '{raw}'")
            self.assertEqual(sym, expected_sym, f"Symbol failed for zero-decimal: '{raw}'")

    def test_unicode_symbols_and_obscure_currencies(self) -> None:
        """Stress-test Unicode currency symbols across Europe, Middle East, Asia, Africa, Americas."""
        cases = [
            ("₺149,99", 149.99, "₺"),
            ("149,99 ₺", 149.99, "₺"),
            ("₴ 249,00", 249.0, "₴"),
            ("249,00 ₴", 249.0, "₴"),
            ("₹ 1,49,000.00", 149000.0, "₹"),
            ("₹1,999", 1999.0, "₹"),
            ("₪79.90", 79.9, "₪"),
            ("79.90 ₪", 79.9, "₪"),
            ("฿299.00", 299.0, "฿"),
            ("299.00 ฿", 299.0, "฿"),
            ("₦14,500.00", 14500.0, "₦"),
            ("89,99 zł", 89.99, "zł"),
            ("zł 89,99", 89.99, "zł"),
            ("149,00 kr", 149.0, "kr"),
            ("kr 149", 149.0, "kr"),
            ("249 Kč", 249.0, "Kč"),
            ("R$ 49,90", 49.9, "R$"),
            ("AED 79.99", 79.99, "AED"),
            ("SAR 99.00", 99.0, "SAR"),
            ("CA$24.99", 24.99, "CA$"),
            ("AU$29.99", 29.99, "AU$"),
            ("NZ$34.99", 34.99, "NZ$"),
            ("HK$158.00", 158.0, "HK$"),
            ("SG$28.98", 28.98, "SG$"),
            ("NT$690", 690.0, "NT$"),
            ("1 490,00 €", 1490.0, "€"),
            ("14 400 ₽", 14400.0, "₽"),
            ("3 990 Ft", 3990.0, "Ft"),
        ]
        for raw, expected_num, expected_sym in cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Failed for Unicode currency: '{raw}'")
            self.assertEqual(sym, expected_sym, f"Symbol failed for Unicode currency: '{raw}'")

    def test_free_tiers_and_zero_values(self) -> None:
        """Assert standard free tiers evaluate to 0.0 with None symbol."""
        free_cases = [
            ("Free", 0.0, None),
            ("FREE", 0.0, None),
            ("free", 0.0, None),
            ("  Free  ", 0.0, None),
            ("$0.00", 0.0, "$"),
            ("0 €", 0.0, "€"),
            ("0.00 $", 0.0, "$"),
            ("¥0", 0.0, "¥"),
        ]
        for raw, expected_num, expected_sym in free_cases:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(num, expected_num, f"Free tier failed for: '{raw}'")
            self.assertEqual(sym, expected_sym, f"Symbol failed for: '{raw}'")

    def test_reproduction_space_separator_variants_bug(self) -> None:
        """Verify Non-breaking space (U+00A0) and thin space (U+2009) parse accurately."""
        # Standard ASCII space passes
        num_ascii, sym_ascii = parse_price_and_currency("1 490,00 €")
        self.assertEqual(num_ascii, 1490.0)
        self.assertEqual(sym_ascii, "€")

        # Non-breaking space U+00A0
        raw_nbsp = "1\u00a0490,00 €"
        num_nbsp, sym_nbsp = parse_price_and_currency(raw_nbsp)
        self.assertEqual(num_nbsp, 1490.0)
        self.assertEqual(sym_nbsp, "€")

        # Thin space U+2009
        raw_thin = "14\u2009400 ₽"
        num_thin, sym_thin = parse_price_and_currency(raw_thin)
        self.assertEqual(num_thin, 14400.0)
        self.assertEqual(sym_thin, "₽")

    def test_reproduction_multiple_period_thousands_separators_bug(self) -> None:
        """Bug reproduction: Multiple periods in European/Vietnamese formats fail to parse.

        E.g. '1.490.000 €' or '500.000.000 ₫':
        Because there is no comma, 'elif \",\" in n:' is skipped,
        leading to float('1.490.000') which throws ValueError -> None.
        """
        raw_multi_dot = "1.490.000 €"
        num, sym = parse_price_and_currency(raw_multi_dot)
        self.assertIsNone(
            num,
            f"Empirically reproduced: multiple periods '1.490.000 €' fails to parse: {num}",
        )

    def test_reproduction_apostrophe_thousands_separator_bug(self) -> None:
        """Bug reproduction: Swiss Franc apostrophe separator 'CHF 1\\'490.00' truncates.

        Regex r'[-+]?\\s*([0-9][0-9.,\\s]*)' stops at the apostrophe,
        extracting only '1' as the number and leaving ''490.00' in the suffix.
        """
        raw_chf = "CHF 1'490.00"
        num, sym = parse_price_and_currency(raw_chf)
        self.assertEqual(
            num,
            1.0,
            f"Empirically reproduced: Swiss Franc '1\\'490.00' truncates to 1.0: {num}",
        )

    def test_reproduction_free_in_promotional_phrasing_zeroes_price(self) -> None:
        """Verify 'Ad-free $9.99' and 'Toll-free $29.99' preserve real prices instead of zeroing."""
        samples = [
            ("Ad-free $9.99", 9.99, "$"),
            ("Toll-free support + $49.99", 49.99, "$"),
            ("Freemium $14.99", 14.99, "$"),
            ("Risk-free trial, then $19.99", 19.99, "$"),
        ]
        for raw, expected_num, expected_sym in samples:
            num, sym = parse_price_and_currency(raw)
            self.assertEqual(
                num,
                expected_num,
                f"Failed for '{raw}'",
            )
            self.assertEqual(sym, expected_sym, f"Failed symbol for '{raw}'")

    def test_reproduction_openrank_pricing_european_comma_decimal_multiplication(self) -> None:
        """Bug reproduction: pricing._parse_usd_price('19,99 €') strips commas, yielding 1999.0."""
        # When local price is used as USD fallback in PricingEngine line 587
        raw_local = "19,99 €"
        val = _parse_usd_price(raw_local)
        self.assertEqual(
            val,
            1999.0,
            f"Empirically reproduced: '19,99 €' becomes 1999.0 in _parse_usd_price: {val}",
        )

    def test_html_entities_and_escaped_currency_symbols(self) -> None:
        """Assert HTML entities unescape properly when passed into parsers."""
        cases = [
            ("&#36;19.99", 19.99, "$"),
            ("&pound;15.99", 15.99, "£"),
            ("&yen;1500", 1500.0, "¥"),
            ("&#8369; 1,490.00", 1490.0, "₱"),
            ("&euro; 19,99", 19.99, "€"),
        ]
        for raw_entity, expected_num, expected_sym in cases:
            unescaped = html.unescape(raw_entity)
            num, sym = parse_price_and_currency(unescaped)
            self.assertEqual(num, expected_num)
            self.assertEqual(sym, expected_sym)


class TestMalformedHtmlAndJsonRobustness(unittest.TestCase):
    """Stress tests asserting zero unhandled exceptions on adversarial and malformed inputs."""

    def test_adversarial_malformed_inputs_never_crash(self) -> None:
        """Assert an extensive corpus of malformed inputs produces no unhandled exceptions."""
        adversarial_corpus = [
            None,
            "",
            "   ",
            "\n\t\r",
            "N/A",
            "--",
            "TBD",
            "$$$$",
            "...",
            ",,,",
            "-",
            "+",
            "-$",
            "$-",
            "127.0.0.1",
            "NaN",
            "Infinity",
            "-Infinity",
            "\x00\x01\x02\x03",
            "Emoji 🚀💎🎉",
            "RTL override \u202e$19.99\u202c",
            "Very long numeric string: " + "9" * 500,
            "Nested brackets: [({$19.99})]",
            "Mixed delimiters: $1,2.3,4.5",
            12345,
            {"price": 10},
            ["$19.99"],
            True,
            False,
        ]

        for item in adversarial_corpus:
            try:
                num, sym = parse_price_and_currency(item)  # type: ignore
                # Should return cleanly without throwing
                self.assertTrue(num is None or isinstance(num, (int, float)))
                self.assertTrue(sym is None or isinstance(sym, str))
            except Exception as e:
                self.fail(f"Unhandled exception on input {repr(item)}: {e}")

    def test_apple_iap_malformed_html_and_json_never_crashes(self) -> None:
        """Assert parse_apple_iap_html never crashes on malformed, corrupted, or hostile HTML/JSON."""
        hostile_html_samples = [
            "",
            None,
            "<script></script>",
            '<script id="serialized-server-data"></script>',
            '<script id="serialized-server-data">{"items": null}</script>',
            '<script id="serialized-server-data">{"title": "In-App Purchases", "items": [{"leadingText": null, "trailingText": null}]}</script>',
            '<script id="serialized-server-data">{"title": "In-App Purchases", "items": [1, 2, 3, "foo", false, null]}</script>',
            '<script id="serialized-server-data">' + '{"nested": ' * 50 + '1' + '}' * 50 + '</script>',
            '<section class="in-app-purchases">' + '<li class="list-item">' * 200,
            '<span class="list-item__title">' * 100 + '$19.99',
            '<<<>>>///&&&;;;"""\'\'\'',
            "\x00" * 1000,
        ]

        for sample in hostile_html_samples:
            try:
                items = parse_apple_iap_html(sample)  # type: ignore
                self.assertIsInstance(items, list)
            except Exception as e:
                self.fail(f"Unhandled exception on HTML sample {repr(sample)[:50]}: {e}")

    def test_batch_performance_sla_under_0_5s(self) -> None:
        """Assert parsing 1,000 diverse currency strings executes well within 0.5s SLA (< 0.05s)."""
        samples = [
            "$19.99",
            "₱ 999.00",
            "¥1,500",
            "€ 1.490,00",
            "149,00 kr",
            "Free",
            "R$ 49,90",
            "₹ 1,49,000.00",
            "₺149,99",
            "CA$24.99/mo",
        ] * 100  # 1,000 samples

        start = time.perf_counter()
        for s in samples:
            parse_price_and_currency(s)
        total_time = time.perf_counter() - start

        # Benchmark SLA: 1,000 parses must finish in < 0.5s
        self.assertLess(
            total_time,
            0.5,
            f"1,000 currency parses took {total_time:.4f}s, exceeding 0.5s SLA",
        )
        # Typically finishes in < 0.02s on modern machines
        self.assertLess(
            total_time,
            0.05,
            f"1,000 currency parses took {total_time:.4f}s, exceeding 0.05s tight SLA",
        )


if __name__ == "__main__":
    unittest.main()
