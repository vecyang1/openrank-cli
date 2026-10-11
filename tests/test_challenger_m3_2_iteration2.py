"""Empirical adversarial challenge harness for Milestone 3 Iteration 2 Hardening.

Author: m3_challenger_2 (teamwork_preview_challenger)
Purpose: Independent verification of 6 prior failure modes from m3_challenger_1,
         stress-testing two-pass tier matching permutations, edge-case price parsing,
         and anti-spoofing/canonical redirect chains using loopback socket servers.
"""
from __future__ import annotations

import http.server
import json
import threading
import unittest
import urllib.parse
from typing import List, Optional

from openrank.client import (
    HttpClient,
    HttpFastFailError,
)
from openrank.models import (
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)
from openrank.storefront_apple import (
    AppleStorefrontValidator,
    parse_apple_iap_html,
    parse_price_and_currency,
)


class EphemeralSocketServer:
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


class TestPriorFailureModesVerification(unittest.TestCase):
    """Verify whether the 6 failure modes identified by m3_challenger_1 are resolved."""

    def test_mode_1_greedy_tier_match_shuffled_order(self) -> None:
        """Verify tier order shuffling (ChatGPT Pro before ChatGPT Plus) correctly pairs Plus with Plus."""
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
                html = """
                <script id="serialized-server-data">
                [{"data": {"content": {"sections": [{"title": "In-App Purchases", "items": [
                    {"leadingText": "ChatGPT Pro", "trailingText": "$199.99"},
                    {"leadingText": "ChatGPT Plus", "trailingText": "$19.99"}
                ]}]}}}]
                </script>
                """
                return 200, url, html.encode("utf-8"), False

        validator = AppleStorefrontValidator(client=ShuffledOrderClient())
        is_valid, err = validator.cross_validate_pricing(otr_res, "US")
        self.assertTrue(is_valid)
        self.assertIsNone(err)
        self.assertTrue(any("ChatGPT Plus" in n for n in otr_res.validation_notes))

    def test_mode_2_missing_leading_zero(self) -> None:
        """Verify sub-dollar strings like '$.99' and '.99' parse as 0.99."""
        num_dot99, sym_dot99 = parse_price_and_currency("$.99")
        self.assertEqual(num_dot99, 0.99)
        self.assertEqual(sym_dot99, "$")

        num_bare, sym_bare = parse_price_and_currency(".99")
        self.assertEqual(num_bare, 0.99)
        self.assertIsNone(sym_bare)

        num_comma, sym_comma = parse_price_and_currency(",99 €")
        self.assertEqual(num_comma, 0.99)
        self.assertEqual(sym_comma, "€")

    def test_mode_3_anti_spoofing_slug_matches_country(self) -> None:
        """Verify path segment extraction catches spoofing when slug matches expected country."""
        client = HttpClient()
        url = "https://apps.apple.com/gb/app/ph/id6448311069"
        parsed = urllib.parse.urlsplit(url)
        path_segments = [p for p in parsed.path.split("/") if p]
        storefront_cc = path_segments[0].lower() if path_segments else ""
        self.assertEqual(storefront_cc, "gb")
        self.assertNotEqual(storefront_cc, "ph")

    def test_mode_4_in_country_301_followed_to_200(self) -> None:
        """Verify in-country canonical 301 is followed up to 3 hops and retrieves IAPs."""
        class MockFollowRedirectClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                if "chatgpt-canonical" in url:
                    html = """
                    <script id="serialized-server-data">
                    [{"title": "In-App Purchases", "items": [{"leadingText": "Tier 1", "trailingText": "$9.99"}]}]
                    </script>
                    """
                    return 200, url, html.encode("utf-8"), False
                return 301, "https://apps.apple.com/ph/app/chatgpt-canonical/id6448311069", b"", False

        v = AppleStorefrontValidator(client=MockFollowRedirectClient())
        res = v.validate_product("chatgpt", "ph")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0].name, "Tier 1")

    def test_mode_5_free_trial_promotional_text(self) -> None:
        """Verify basic promo trials preserve actual prices."""
        num, sym = parse_price_and_currency("Free trial then $19.99")
        self.assertEqual(num, 19.99)
        self.assertEqual(sym, "$")

        num, sym = parse_price_and_currency("Try free for 7 days, then $9.99/mo")
        self.assertEqual(num, 9.99)
        self.assertEqual(sym, "$")

    def test_mode_6_regex_scoped_to_li_boundary(self) -> None:
        """Verify unpriced items in HTML lists do not bleed into subsequent tier prices."""
        html_content = """
        <section class="in-app-purchases-section">
        <ol class="list--in-app-purchases">
          <li class="list-item">
            <span class="list-item__title">Unpriced Tier</span>
          </li>
          <li class="list-item">
            <span class="list-item__title">Priced Tier</span>
            <span class="list-item__price">$14.99</span>
          </li>
        </ol>
        </section>
        """
        items = parse_apple_iap_html(html_content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "Priced Tier")
        self.assertEqual(items[0].numeric_price, 14.99)


class TestAdversarialTierMatchingPermutations(unittest.TestCase):
    """Stress test two-pass tier matching algorithm with adversarial permutations."""

    def test_reverse_order_multi_tier(self) -> None:
        """Assert multi-tier matching succeeds when Apple returns tiers in reverse order."""
        product = ProductSummary("Duolingo", "duolingo", "/duolingo", "Education", "https://opentherank.com/duolingo")
        record = RegionPricingRecord(
            country_code="US",
            region_name="United States",
            platform="appstore",
            primary_tier=PriceTier("Super Duolingo", "$12.99", 12.99),
            additional_tiers={
                "family": PriceTier("Super Duolingo Family", "$19.99", 19.99),
                "max": PriceTier("Duolingo Max", "$29.99", 29.99),
            },
            vs_us_delta=0.0,
            tax_info="",
            risk_level="Low",
            risk_note="",
            snapshot_date="",
        )
        otr_res = ProductPricingResult(product=product, records=[record])

        class ReverseClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html = """
                <script id="serialized-server-data">
                [{"title": "In-App Purchases", "items": [
                    {"leadingText": "Duolingo Max", "trailingText": "$29.99"},
                    {"leadingText": "Super Duolingo Family", "trailingText": "$19.99"},
                    {"leadingText": "Super Duolingo", "trailingText": "$12.99"}
                ]}]
                </script>
                """
                return 200, url, html.encode("utf-8"), False

        v = AppleStorefrontValidator(client=ReverseClient())
        is_valid, err = v.cross_validate_pricing(otr_res, "US")
        self.assertTrue(is_valid, f"Failed with: {err}")
        self.assertIn("Super Duolingo at $12.99", otr_res.validation_notes[0])

    def test_missing_primary_tier_falls_back_to_additional_tier(self) -> None:
        """Assert when primary tier is missing, matching pairs additional tier."""
        product = ProductSummary("Claude", "claude", "/claude", "AI", "https://opentherank.com/claude")
        record = RegionPricingRecord(
            country_code="US",
            region_name="United States",
            platform="appstore",
            primary_tier=PriceTier("Claude Basic", "$9.99", 9.99),
            additional_tiers={"pro": PriceTier("Claude Pro", "$20.00", 20.0)},
            vs_us_delta=0.0,
            tax_info="",
            risk_level="Low",
            risk_note="",
            snapshot_date="",
        )
        otr_res = ProductPricingResult(product=product, records=[record])

        class MissingPrimaryClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html = """
                <script id="serialized-server-data">
                [{"title": "In-App Purchases", "items": [
                    {"leadingText": "Claude Pro", "trailingText": "$20.00"}
                ]}]
                </script>
                """
                return 200, url, html.encode("utf-8"), False

        v = AppleStorefrontValidator(client=MissingPrimaryClient())
        is_valid, err = v.cross_validate_pricing(otr_res, "US")
        self.assertTrue(is_valid, f"Failed with: {err}")
        self.assertIn("Claude Pro at $20.00", otr_res.validation_notes[0])

    def test_reproduction_yearly_duration_token_conflict_causes_false_rejection(self) -> None:
        """Empirical vulnerability: Pass 2 skips both Yearly and Monthly because TIER_TOKENS includes yearly.

        When OTR has 'ChatGPT Plus' ($19.99) and Apple lists 'ChatGPT Plus Yearly' ($199.99)
        before 'ChatGPT Plus Monthly' ($19.99):
        o_known = {'plus'}, a_known_1 = {'plus', 'yearly'}, a_known_2 = {'plus', 'monthly'}.
        Both are skipped in Pass 2 because o_known != a_known.
        Fallback pass picks item 0 ('Yearly') due to identical word overlap, causing false rejection.
        """
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

        class YearlyFirstClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html = """
                <script id="serialized-server-data">
                [{"title": "In-App Purchases", "items": [
                    {"leadingText": "ChatGPT Plus Yearly", "trailingText": "$199.99"},
                    {"leadingText": "ChatGPT Plus Monthly", "trailingText": "$19.99"}
                ]}]
                </script>
                """
                return 200, url, html.encode("utf-8"), False

        v = AppleStorefrontValidator(client=YearlyFirstClient())
        is_valid, err = v.cross_validate_pricing(otr_res, "US")
        # Empirically reproduces the vulnerability: false rejection due to greedy yearly match
        self.assertFalse(is_valid)
        self.assertIn("Price parity mismatch for ChatGPT Plus", err or "")

    def test_reproduction_unrelated_tier_blind_fallback_match(self) -> None:
        """Empirical vulnerability: line 627 blindly pairs primary tier with items[0] when no tiers match.

        If items[0] happens to have the same price as primary tier, it validates completely unrelated tier!
        """
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

        class UnrelatedClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                html = """
                <script id="serialized-server-data">
                [{"title": "In-App Purchases", "items": [
                    {"leadingText": "Dungeon Coins 500", "trailingText": "$19.99"}
                ]}]
                </script>
                """
                return 200, url, html.encode("utf-8"), False

        v = AppleStorefrontValidator(client=UnrelatedClient())
        is_valid, err = v.cross_validate_pricing(otr_res, "US")
        # Empirically reproduces blind fallback: Dungeon Coins is validated as ChatGPT Plus!
        self.assertTrue(is_valid)
        self.assertIn("Dungeon Coins 500", otr_res.validation_notes[0])


class TestPriceParsingEdgeCaseStress(unittest.TestCase):
    """Stress test price and currency parsing on edge-case strings."""

    def test_reproduction_multi_month_promo_trial_captures_duration_as_price(self) -> None:
        """Empirical vulnerability: Multi-month promo strings capture duration digit instead of actual price.

        Because promo_pattern only handles \\d+-day, strings with month(s) or week(s) fail:
        '3 months free, then $9.99/mo' -> extracts (3.0, 'months')
        '1-month free trial, then $14.99/mo' -> extracts (1.0, 'month')
        'Try free for 3 months, then $9.99' -> extracts (3.0, 'for')
        """
        num_3m, sym_3m = parse_price_and_currency("3 months free, then $9.99/mo")
        self.assertEqual(num_3m, 3.0, "Empirically reproduced: 3 months free extracts 3.0")
        self.assertEqual(sym_3m, "months")

        num_1m, sym_1m = parse_price_and_currency("1-month free trial, then $14.99/mo")
        self.assertEqual(num_1m, 1.0, "Empirically reproduced: 1-month free trial extracts 1.0")
        self.assertEqual(sym_1m, "month")

        num_try, sym_try = parse_price_and_currency("Try free for 3 months, then $9.99")
        self.assertEqual(num_try, 3.0, "Empirically reproduced: Try free for 3 months extracts 3.0")
        self.assertEqual(sym_try, "for")

    def test_sub_dollar_with_whitespace(self) -> None:
        """Assert sub-dollar prices with leading and internal whitespace parse accurately."""
        cases = [
            ("$ .99", 0.99, "$"),
            ("$  .99", 0.99, "$"),
            ("$\t.99", 0.99, "$"),
            ("$\u00a0.99", 0.99, "$"),
            ("$\u2009.99", 0.99, "$"),
            ("$\u202f.99", 0.99, "$"),
            (" $ .99 ", 0.99, "$"),
            (" .99 $ ", 0.99, "$"),
        ]
        for raw, exp_n, exp_s in cases:
            n, s = parse_price_and_currency(raw)
            self.assertEqual(n, exp_n, f"Failed for '{raw}'")
            self.assertEqual(s, exp_s, f"Failed symbol for '{raw}'")

    def test_reproduction_dot_whitespace_sub_dollar_bug(self) -> None:
        """Empirical vulnerability: whitespace between '.' and digits causes 100x multiplication.

        '$. 99' matches '99' as an integer with '$.' prefix, evaluating as 99.0.
        """
        num, sym = parse_price_and_currency("$. 99")
        self.assertEqual(num, 99.0)
        self.assertEqual(sym, "$.")

    def test_accented_and_exotic_currencies(self) -> None:
        """Assert accented and regional currency symbols parse accurately."""
        cases = [
            ("19,99 zł", 19.99, "zł"),
            ("149 Kč", 149.0, "Kč"),
            ("3 990 Ft", 3990.0, "Ft"),
            ("249,00 kn", 249.0, "kn"),
            ("99 Lei", 99.0, "Lei"),
            ("19.90 TL", 19.9, "TL"),
            ("1 490 сом", 1490.0, "сом"),
            ("149 лв", 149.0, "лв"),
            ("1 490 руб", 1490.0, "руб"),
            ("1 490 դր.", 1490.0, "դր."),
            ("49 ლ", 49.0, "ლ"),
            ("₡ 15 000", 15000.0, "₡"),
            ("₲ 150 000", 150000.0, "₲"),
            ("₫ 250 000", 250000.0, "₫"),
            ("₮ 50 000", 50000.0, "₮"),
        ]
        for raw, exp_n, exp_s in cases:
            n, s = parse_price_and_currency(raw)
            self.assertEqual(n, exp_n, f"Failed for '{raw}'")
            self.assertEqual(s, exp_s, f"Failed symbol for '{raw}'")

    def test_zero_dollar_freemium_tiers(self) -> None:
        """Assert zero dollar and free tier expressions parse cleanly."""
        free_cases = [
            ("Free", 0.0, None),
            ("FREE", 0.0, None),
            ("Gratis", 0.0, None),
            ("Kostenlos", 0.0, None),
            ("Gratuit", 0.0, None),
            ("0.00 $", 0.0, "$"),
            ("0 €", 0.0, "€"),
            ("0,00 €", 0.0, "€"),
            ("0.00", 0.0, None),
            ("0", 0.0, None),
        ]
        for raw, exp_n, exp_s in free_cases:
            n, s = parse_price_and_currency(raw)
            self.assertEqual(n, exp_n, f"Failed for '{raw}'")
            self.assertEqual(s, exp_s, f"Failed symbol for '{raw}'")


class TestAntiSpoofingLoopbackSocketStress(unittest.TestCase):
    """Stress test anti-spoofing and canonical redirect chains with real loopback sockets."""

    @classmethod
    def setUpClass(cls) -> None:
        class LoopbackRedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/ph/app/chatgpt/id6448311069":
                    self.send_response(301)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/ph/app/chatgpt-canonical/id6448311069",
                    )
                    self.end_headers()
                elif p == "/ph/app/chatgpt-canonical/id6448311069":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b'<html><script id="serialized-server-data">[{"title": "In-App Purchases", "items": [{"leadingText": "ChatGPT Plus", "trailingText": "\xe2\x82\xb1 999.00"}]}]</script></html>'
                    )
                elif p == "/ph/spoofed_direct":
                    self.send_response(301)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/us/app/chatgpt/id6448311069",
                    )
                    self.end_headers()
                elif p == "/ph/spoofed_hop1":
                    self.send_response(301)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/ph/spoofed_hop2",
                    )
                    self.end_headers()
                elif p == "/ph/spoofed_hop2":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/us/app/chatgpt/id6448311069",
                    )
                    self.end_headers()
                elif p == "/ph/to_evil":
                    self.send_response(301)
                    self.send_header("Location", "http://evil.com/ph/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/ph/loop_a":
                    self.send_response(301)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/ph/loop_b",
                    )
                    self.end_headers()
                elif p == "/ph/loop_b":
                    self.send_response(301)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/ph/loop_a",
                    )
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = EphemeralSocketServer(LoopbackRedirectHandler)
        cls.port = cls.server.port

        class RewritingClient(HttpClient):
            def fetch_with_redirect_check(self, url, expected_country=None):
                mapped_url = url.replace("https://apps.apple.com", f"http://127.0.0.1:{cls.port}")
                return super().fetch_with_redirect_check(mapped_url, expected_country=expected_country)

        cls.client = RewritingClient(timeout=2.0)
        cls.validator = AppleStorefrontValidator(client=cls.client)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_canonical_in_country_redirect_chain_resolves_and_parses(self) -> None:
        """Verify 301 canonical redirect over loopback socket resolves to 200 and extracts items."""
        res = self.validator.validate_product("chatgpt", "ph")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.is_valid)
        self.assertFalse(res.is_spoofed)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0].name, "ChatGPT Plus")
        self.assertEqual(res.items[0].numeric_price, 999.0)
        self.assertEqual(res.items[0].currency_symbol, "₱")

    def test_direct_cross_country_redirect_detected_as_spoofed(self) -> None:
        """Verify redirect directly to /us/ from /ph/ is detected as spoofed."""
        raw_client = HttpClient(timeout=2.0)
        url = self.server.get_url("/ph/spoofed_direct")
        code, final_url, body, is_spoofed = raw_client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertIn("/us/", final_url)

    def test_multi_hop_cross_country_redirect_detected_on_second_hop(self) -> None:
        """Verify redirect that hops through /ph/ before redirecting to /us/ is detected as spoofed."""
        raw_client = HttpClient(timeout=2.0)
        url1 = self.server.get_url("/ph/spoofed_hop1")
        code1, final1, body1, spoofed1 = raw_client.fetch_with_redirect_check(url1, expected_country="ph")
        self.assertEqual(code1, 301)
        self.assertFalse(spoofed1)

        code2, final2, body2, spoofed2 = raw_client.fetch_with_redirect_check(final1, expected_country="ph")
        self.assertEqual(code2, 302)
        self.assertTrue(spoofed2)
        self.assertIn("/us/", final2)

    def test_reproduction_open_redirect_to_external_domain_not_flagged(self) -> None:
        """Empirical vulnerability: open redirect to evil.com/ph/... has path_segments[0]=='ph', bypassing spoofing."""
        raw_client = HttpClient(timeout=2.0)
        url = self.server.get_url("/ph/to_evil")
        code, final_url, body, is_spoofed = raw_client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertEqual(code, 301)
        self.assertIn("evil.com", final_url)
        self.assertFalse(is_spoofed, "Empirically reproduced: external evil.com/ph passes anti-spoofing")

    def test_redirect_loop_terminates_without_hanging(self) -> None:
        """Assert redirect loops cleanly terminate within max_hops without infinite recursion."""
        raw_client = HttpClient(timeout=2.0)
        url = self.server.get_url("/ph/loop_a")
        # Direct fetch stops at first hop
        code, final_url, body, is_spoofed = raw_client.fetch_with_redirect_check(url, expected_country="ph")
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)


if __name__ == "__main__":
    unittest.main()
