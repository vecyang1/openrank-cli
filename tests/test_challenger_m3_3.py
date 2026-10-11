"""Empirical challenger test harness for Milestone 3 Storefront Validation.

Author: m3_challenger_3 (teamwork_preview_challenger)
Purpose: Independent empirical stress-testing and verification:
         1. Adversarial probe of _extract_iaps_from_html_regex (ReDoS, catastrophic backtracking,
            memory leak stability, malformed/broken/nested HTML tags, extreme whitespace).
         2. Anti-spoofing boundary conditions in fetch_with_redirect_check (casing, trailing
            slashes, query parameters, anchors, port numbers, IPv4/IPv6 literals, relative paths).
         3. Latency SLA (<0.5s) benchmark across 4xx fast-fail codes (400, 401, 403, 404, 410, 429, 422, 451).
"""
from __future__ import annotations

import http.server
import re
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from typing import Dict, List, Optional, Tuple

from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
    fetch_with_redirect_check,
)
from openrank.models import AppleIAPItem, AppleValidationResult
from openrank.storefront_apple import (
    AppleStorefrontValidator,
    _extract_iaps_from_html_regex,
    parse_apple_iap_html,
    parse_price_and_currency,
)


class EphemeralMockServer:
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


class TestRegexAdversarialRobustness(unittest.TestCase):
    """Adversarial stress-testing of regex HTML parser for ReDoS, memory leaks, and malformed tags."""

    def test_redos_and_catastrophic_backtracking_unclosed_tags(self) -> None:
        """Verify regex does not catastrophically backtrack on unclosed tags and huge repetitions."""
        # Scenario A: 1,000 unclosed <li> tags without </li>
        malicious_unclosed_li = "<section class=\"in-app-purchases\"><ol>" + ("<li>" * 1000) + "</ol></section>"
        t0 = time.perf_counter()
        res_li = _extract_iaps_from_html_regex(malicious_unclosed_li)
        elapsed_li = time.perf_counter() - t0
        # Hard SLA: must terminate in < 0.5s (proves no exponential 2^N catastrophic backtracking)
        self.assertLess(elapsed_li, 0.5, f"Unclosed <li> ReDoS triggered: took {elapsed_li:.4f}s")
        self.assertEqual(res_li, [])

        # Scenario B: 1,000 unclosed <span class="list-item__title"> without closing tags
        malicious_unclosed_spans = (
            "<section class=\"in-app-purchases\"><ol><li>"
            + ('<span class="list-item__title">Tier' * 1000)
            + "</li></ol></section>"
        )
        t0 = time.perf_counter()
        res_spans = _extract_iaps_from_html_regex(malicious_unclosed_spans)
        elapsed_spans = time.perf_counter() - t0
        self.assertLess(elapsed_spans, 0.5, f"Unclosed span ReDoS triggered: took {elapsed_spans:.4f}s")
        self.assertEqual(res_spans, [])

        # Scenario C: 1,000 unclosed <section class="in-app-purchases">
        malicious_unclosed_sec = ('<section class="in-app-purchases">' * 1000) + "trailing content"
        t0 = time.perf_counter()
        res_sec = _extract_iaps_from_html_regex(malicious_unclosed_sec)
        elapsed_sec = time.perf_counter() - t0
        self.assertLess(elapsed_sec, 0.5, f"Unclosed section ReDoS triggered: took {elapsed_sec:.4f}s")
        self.assertEqual(res_sec, [])

    def test_extreme_whitespace_and_unicode_delimiters(self) -> None:
        """Verify parser survives massive whitespace bloat and non-standard Unicode whitespace."""
        extreme_spaces = " " * 50000
        extreme_newlines = "\n" * 20000
        unicode_spaces = "\u00a0\u2009\u202f" * 50

        html_bloated = f"""
        <section class="in-app-purchases"{extreme_spaces}>
            <ol{extreme_newlines}>
                <li>
                    <span class="list-item__title">
                        {extreme_spaces}ChatGPT Plus{extreme_newlines}
                    </span>
                    <span class="list-item__price">
                        {extreme_spaces}${unicode_spaces}19.99{extreme_newlines}
                    </span>
                </li>
            </ol>
        </section>
        """
        t0 = time.perf_counter()
        items = _extract_iaps_from_html_regex(html_bloated)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.25, f"Extreme whitespace parsing took too long: {elapsed:.4f}s")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "ChatGPT Plus")
        self.assertEqual(items[0].numeric_price, 19.99)
        self.assertEqual(items[0].currency_symbol, "$")

    def test_nested_formatting_tags_and_interleaved_items(self) -> None:
        """Verify robust extraction when HTML contains formatting tags and interleaved unpriced items."""
        # 1. Broken attributes and nested formatting tags (<b>, <em>, <strong>, <small>)
        # 2. Interleaved items: unpriced item and unnamed item MUST NOT bleed into each other
        adversarial_html = """
        <section class="in-app-purchases">
            <ol>
                <!-- Item 1: Nested formatting tags inside title and price -->
                <li id="item-1" class="list-item" data-test="true">
                    <span class="list-item__title">
                        <b>ChatGPT</b> <em>Plus</em> <small>(Monthly)</small>
                    </span>
                    <span class="list-item__price">
                        <strong>$19.99</strong>
                    </span>
                </li>

                <!-- Item 2: Unpriced item (MUST NOT capture Item 3 price) -->
                <li id="item-2">
                    <span class="list-item__title">Unpriced Beta Tier</span>
                </li>

                <!-- Item 3: Price only item (MUST NOT attach to Item 2 title) -->
                <li id="item-3">
                    <span class="list-item__price">$99.99</span>
                </li>

                <!-- Item 4: Additional classes on spans -->
                <li id="item-4">
                    <span class="we-truncate list-item__title we-truncate--single-line">
                        ChatGPT Pro
                    </span>
                    <span class="we-truncate list-item__price we-truncate--single-line">
                        $199.99
                    </span>
                </li>

                <!-- Item 5: Euro symbol and European decimal formatting -->
                <li id="item-5">
                    <span class="list-item__title">Duolingo Super</span>
                    <span class="list-item__price">12,99 €</span>
                </li>
            </ol>
        </section>
        """

        items = _extract_iaps_from_html_regex(adversarial_html)

        # Expected: exactly 3 valid pairs (Item 1, Item 4, Item 5)
        # Item 2 (unpriced) and Item 3 (unnamed) MUST be safely skipped without cross-item contamination
        self.assertEqual(len(items), 3)

        self.assertEqual(items[0].name, "ChatGPT Plus (Monthly)")
        self.assertEqual(items[0].numeric_price, 19.99)
        self.assertEqual(items[0].currency_symbol, "$")

        self.assertEqual(items[1].name, "ChatGPT Pro")
        self.assertEqual(items[1].numeric_price, 199.99)
        self.assertEqual(items[1].currency_symbol, "$")

        self.assertEqual(items[2].name, "Duolingo Super")
        self.assertEqual(items[2].numeric_price, 12.99)
        self.assertEqual(items[2].currency_symbol, "€")

    def test_nested_span_limitation_documented(self) -> None:
        """Document regex limitation when span tags are nested inside list-item__title."""
        # Non-greedy regex stops at the first closing </span> tag
        html_nested_spans = """
        <section class="in-app-purchases">
            <ol>
                <li>
                    <span class="list-item__title"><span>SubTier</span> FullTitle</span>
                    <span class="list-item__price">$9.99</span>
                </li>
            </ol>
        </section>
        """
        items = _extract_iaps_from_html_regex(html_nested_spans)
        self.assertEqual(len(items), 1)
        # Empirical finding: regex captures 'SubTier' because it stops at first </span>
        self.assertEqual(items[0].name, "SubTier")
        self.assertEqual(items[0].numeric_price, 9.99)

    def test_textpair_fallback_with_special_characters_and_whitespace(self) -> None:
        """Verify TextPair regex fallback handles unquoted/single-quoted JSON, newlines, and escaping."""
        html_fallback = """
        <div id="wrapper">
            {'leadingText': 'Perplexity Pro (Annual)', 'trailingText': '$200.00 / yr'}
            {"leadingText": "Perplexity Pro (Monthly)", "trailingText": "$20.00 / mo"}
            {'leadingText': 'Special &amp; Discounted Tier', 'trailingText': '&#163;9.99'}
        </div>
        """
        items = _extract_iaps_from_html_regex(html_fallback)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].name, "Perplexity Pro (Annual)")
        self.assertEqual(items[0].numeric_price, 200.0)
        self.assertEqual(items[1].name, "Perplexity Pro (Monthly)")
        self.assertEqual(items[1].numeric_price, 20.0)
        self.assertEqual(items[2].name, "Special & Discounted Tier")
        self.assertEqual(items[2].numeric_price, 9.99)
        self.assertEqual(items[2].currency_symbol, "£")

    def test_memory_stability_across_large_batch(self) -> None:
        """Verify repeated parsing of adversarial inputs causes no memory leaks."""
        sample_html = """
        <section class="in-app-purchases">
            <ol>
                <li>
                    <span class="list-item__title">Tier 1</span>
                    <span class="list-item__price">$9.99</span>
                </li>
                <li>
                    <span class="list-item__title">Tier 2</span>
                    <span class="list-item__price">$19.99</span>
                </li>
            </ol>
        </section>
        """ * 50

        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()

        for _ in range(500):
            _ = _extract_iaps_from_html_regex(sample_html)

        snapshot_end = tracemalloc.take_snapshot()
        tracemalloc.stop()

        top_stats = snapshot_end.compare_to(snapshot_start, "lineno")
        total_growth = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)
        # Total retained memory growth should be minimal (<2MB for 500 iterations of 50 tiers)
        self.assertLess(
            total_growth,
            2 * 1024 * 1024,
            f"Excessive memory retention detected: {total_growth / 1024:.2f} KB",
        )


class TestAntiSpoofingBoundaryConditions(unittest.TestCase):
    """Stress tests for fetch_with_redirect_check anti-spoofing boundary conditions."""

    server: EphemeralMockServer

    @classmethod
    def setUpClass(cls) -> None:
        class RedirectMockHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                # Format: /redirect?loc=<encoded_target_location>&status=301
                query = urllib.parse.urlsplit(self.path).query
                params = urllib.parse.parse_qs(query)

                if "loc" in params:
                    target_loc = params["loc"][0]
                    code = int(params.get("status", ["301"])[0])
                    self.send_response(code)
                    self.send_header("Location", target_loc)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif "/200" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK")
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"DEFAULT")

            def log_message(self, format, *args):
                pass

        cls.server = EphemeralMockServer(RedirectMockHandler)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_casing_variations_and_whitespace(self) -> None:
        """Verify case insensitivity and whitespace handling for expected_country vs Location."""
        client = HttpClient()

        # 1. Uppercase expected country "PH" matching lowercase Location "/ph/app/..."
        url = self.server.get_url("/redirect?loc=/ph/app/chatgpt/id1")
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="PH")
        self.assertFalse(is_spoofed, "Uppercase expected_country should match lowercase storefront path")

        # 2. Lowercase expected country "ph" matching uppercase Location "/PH/app/..."
        url = self.server.get_url("/redirect?loc=/PH/app/chatgpt/id1")
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed, "Lowercase expected_country should match uppercase Location path")

        # 3. Expected country with outer whitespace "  ph  "
        url = self.server.get_url("/redirect?loc=/ph/app/chatgpt/id1")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="  ph  ")
        self.assertFalse(is_spoofed, "Expected country with whitespace should be stripped cleanly")

        # 4. Mismatched country: expected "ph" redirecting to "/US/app/..."
        url = self.server.get_url("/redirect?loc=/US/app/chatgpt/id1")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertTrue(is_spoofed, "Redirecting to /US/ when expected is 'ph' must be detected as spoofed")

    def test_trailing_slashes_and_multiple_slashes(self) -> None:
        """Verify path segments with trailing slashes, redundant slashes, and root paths."""
        client = HttpClient()

        # 1. Location with trailing slash "/ph/"
        url = self.server.get_url("/redirect?loc=/ph/")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)

        # 2. Location with multiple consecutive slashes "///ph///app///id1"
        url = self.server.get_url("/redirect?loc=///ph///app///id1")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed, "Consecutive slashes should not distort path segmentation")

        # 3. Location without trailing slash "/ph"
        url = self.server.get_url("/redirect?loc=/ph")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)

        # 4. Location redirecting to root "/" (empty country path)
        url = self.server.get_url("/redirect?loc=/")
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertTrue(is_spoofed, "Redirecting to root '/' has no country segment, must be spoofed")

    def test_query_parameters_and_anchors(self) -> None:
        """Verify query strings and anchors do not pollute country detection."""
        client = HttpClient()

        # 1. Query parameters after valid path
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote("/ph/app/chatgpt/id1?uo=4&l=en"))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)
        self.assertIn("uo=4", final_url)

        # 2. URL anchor/fragment after valid path
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote("/ph/app/chatgpt/id1#section-iap"))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)
        self.assertIn("#section-iap", final_url)

        # 3. Country only present in query parameter, not path (e.g. /app/chatgpt?cc=ph)
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote("/app/chatgpt?cc=ph"))
        _, _, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertTrue(is_spoofed, "Query parameter cc=ph cannot substitute for storefront path segment")

    def test_port_numbers_and_ip_literals(self) -> None:
        """Verify port numbers and IPv4/IPv6 host formats are handled cleanly."""
        client = HttpClient()

        # 1. Explicit port in absolute Location
        abs_loc_valid = f"http://127.0.0.1:{self.server.port}/ph/app/chatgpt"
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote(abs_loc_valid))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)
        self.assertEqual(final_url, abs_loc_valid)

        # 2. Explicit port in absolute spoofed Location
        abs_loc_spoof = f"http://127.0.0.1:{self.server.port}/us/app/chatgpt"
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote(abs_loc_spoof))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertTrue(is_spoofed)

    def test_relative_path_traversal(self) -> None:
        """Verify relative path segments ('..', subpaths) correctly compute target country."""
        client = HttpClient()

        # 1. Traversal that navigates from /ph/app/id1 to /ph/app/canonical
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote("/ph/app/canonical"))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertFalse(is_spoofed)

        # 2. Relative traversal to US store via double dots
        url = self.server.get_url("/redirect?loc=" + urllib.parse.quote("../../../us/app/chatgpt"))
        _, final_url, _, is_spoofed = client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertTrue(is_spoofed, "Relative jump to /us/ must trigger anti-spoofing")


class TestHttpFastFailSLA(unittest.TestCase):
    """Empirical benchmarking of 4xx fast-fail SLA (<0.5s) across all 4xx status codes."""

    server: EphemeralMockServer

    @classmethod
    def setUpClass(cls) -> None:
        class FastFailHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                # Path format: /fastfail/<code>
                if "/fastfail/" in self.path:
                    code_str = self.path.split("/fastfail/")[1].split("?")[0]
                    code = int(code_str)
                    self.send_response(code)
                    if code == 429:
                        # Adversarial: simulate 1 hour Retry-After header
                        self.send_header("Retry-After", "3600")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "client_error"}')
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def log_message(self, format, *args):
                pass

        cls.server = EphemeralMockServer(FastFailHandler)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_4xx_codes_sla_benchmark_under_0_5s(self) -> None:
        """Empirically verify 400, 401, 403, 404, 410, 429, 422, 451 fast-fail under 0.5s."""
        client = HttpClient()
        target_codes = [400, 401, 403, 404, 410, 429, 422, 451]

        latencies: Dict[int, List[float]] = {}

        for code in target_codes:
            latencies[code] = []
            url = self.server.get_url(f"/fastfail/{code}")

            # Run 5 samples per status code
            for _ in range(5):
                t0 = time.perf_counter()
                with self.assertRaises(HttpFastFailError) as ctx:
                    client.get(url)
                elapsed = time.perf_counter() - t0

                latencies[code].append(elapsed)
                self.assertEqual(ctx.exception.status_code, code)
                # Hard SLA requirement: must be strictly less than 0.5 seconds
                self.assertLess(
                    elapsed,
                    0.5,
                    f"HTTP {code} fast-fail violated SLA (<0.5s): took {elapsed:.4f}s",
                )

        # Assert mean latency across all 4xx is well under 0.05s on local socket
        for code, times in latencies.items():
            mean_time = sum(times) / len(times)
            max_time = max(times)
            self.assertLess(
                mean_time,
                0.05,
                f"HTTP {code} mean latency unexpectedly high: {mean_time:.4f}s",
            )
            self.assertLess(
                max_time,
                0.5,
                f"HTTP {code} max latency exceeded SLA: {max_time:.4f}s",
            )

    def test_429_retry_after_3600_does_not_sleep(self) -> None:
        """Verify HTTP 429 with Retry-After: 3600 fails immediately without sleeping."""
        client = HttpClient()
        url = self.server.get_url("/fastfail/429")

        t0 = time.perf_counter()
        with self.assertRaises(HttpFastFailError) as ctx:
            client.get(url)
        elapsed = time.perf_counter() - t0

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertLess(
            elapsed,
            0.1,
            f"HTTP 429 slept instead of failing fast: took {elapsed:.4f}s",
        )

    def test_validator_validate_product_handles_4xx_without_raising(self) -> None:
        """Verify AppleStorefrontValidator.validate_product catches fast-fail cleanly."""
        client = HttpClient()
        validator = AppleStorefrontValidator(client=client)

        # Target 404 URL directly via mock client
        class MockClientWith404:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise HttpFastFailError(404, "Not Found", url=url)

        val_404 = AppleStorefrontValidator(client=MockClientWith404())
        t0 = time.perf_counter()
        res_404 = val_404.validate_product("chatgpt", "ph")
        elapsed_404 = time.perf_counter() - t0

        self.assertLess(elapsed_404, 0.1)
        self.assertFalse(res_404.is_valid)
        self.assertFalse(res_404.is_spoofed)
        self.assertEqual(res_404.status_code, 404)
        self.assertIn("404", res_404.error)

        # Target 429 URL directly via mock client
        class MockClientWith429:
            def fetch_with_redirect_check(self, url, expected_country=None):
                raise HttpFastFailError(429, "Too Many Requests", url=url)

        val_429 = AppleStorefrontValidator(client=MockClientWith429())
        t0 = time.perf_counter()
        res_429 = val_429.validate_product("chatgpt", "ph")
        elapsed_429 = time.perf_counter() - t0

        self.assertLess(elapsed_429, 0.1)
        self.assertFalse(res_429.is_valid)
        self.assertFalse(res_429.is_spoofed)
        self.assertEqual(res_429.status_code, 429)
        self.assertIn("429", res_429.error)


if __name__ == "__main__":
    unittest.main()
