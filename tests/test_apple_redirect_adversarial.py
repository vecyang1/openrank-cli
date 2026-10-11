"""Empirical adversarial stress test suite for Apple CDN redirect anti-spoofing detection.

Covers:
1. Relative Location paths (absolute paths, relative paths, query strings, path traversal).
2. Port numbers (standard, non-standard, ephemeral socket ports).
3. Protocol / scheme changes (HTTP to HTTPS upgrade, HTTPS to HTTP downgrade).
4. Case sensitivity (country codes: PH, ph, Ph, ' pH '; Location header: /US/, /us/, /Us/).
5. In-country canonical redirects (slug updates, trailing slashes).
6. Cross-country redirects to third countries (PH -> GB, CA -> AU, DE -> AT).
7. Evasion edge-cases (app slug matching country code 'ph', app slug containing 'us', US redirected to GB).
8. Assertions on AppleValidationResult (is_spoofed=True, is_valid=False, error populated).
"""
from __future__ import annotations

import http.server
import threading
import unittest
from pathlib import Path
from typing import Optional

from openrank.client import (
    HttpClient,
    HttpFastFailError,
    fetch_with_redirect_check,
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
    AppleStorefrontValidator,
    cross_validate_pricing,
    validate_product,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class EphemeralRedirectServer:
    """Ephemeral HTTP server for deterministic loopback socket tests."""

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


class TestRelativeLocationPaths(unittest.TestCase):
    """Category 1: Relative Location header paths."""

    @classmethod
    def setUpClass(cls) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/test-root-relative-us":
                    self.send_response(301)
                    self.send_header("Location", "/us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/test-root-relative-ph":
                    self.send_response(301)
                    self.send_header("Location", "/ph/app/chatgpt-canonical/id6448311069")
                    self.end_headers()
                elif p.endswith("/test-traversal-to-us"):
                    self.send_response(302)
                    self.send_header("Location", "../../us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/test-query-only":
                    self.send_response(302)
                    self.send_header("Location", "?l=en")
                    self.end_headers()
                elif p == "/test-relative-slug":
                    self.send_response(301)
                    self.send_header("Location", "chatgpt-official/id6448311069")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def log_message(self, *args):
                pass

        cls.server = EphemeralRedirectServer(Handler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_relative_root_path_to_us_detected_as_spoofed(self) -> None:
        """Assert Location: /us/app/... is detected as is_spoofed=True and final_url is absolute."""
        url = self.server.get_url("/test-root-relative-us")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertTrue(final_url.startswith("http://127.0.0.1:"))
        self.assertIn("/us/app/chatgpt/id6448311069", final_url)

    def test_relative_root_path_in_country_not_spoofed(self) -> None:
        """Assert Location: /ph/app/... preserves country and is_spoofed=False."""
        url = self.server.get_url("/test-root-relative-ph")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)
        self.assertIn("/ph/app/chatgpt-canonical/id6448311069", final_url)

    def test_relative_path_traversal_to_us_detected(self) -> None:
        """Assert Location: ../../us/app/... is detected as is_spoofed=True."""
        url = self.server.get_url("/ph/app/test-traversal-to-us")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 302)
        self.assertTrue(is_spoofed)
        self.assertIn("/us/app/chatgpt/id6448311069", final_url)

    def test_query_string_relative_redirect_behavior(self) -> None:
        """Empirical probe: Location: ?l=en relative query redirect.

        Evaluates whether query-only redirect without country in Location header
        triggers false positive spoof detection because location string lacks /{exp}/.
        """
        url = self.server.get_url("/ph/app/chatgpt/id6448311069")
        # Direct check on client with simulated relative query header
        class QueryRelHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "?l=en")
                self.end_headers()

            def log_message(self, *args):
                pass

        q_server = EphemeralRedirectServer(QueryRelHandler)
        try:
            target_url = q_server.get_url("/ph/app/chatgpt/id6448311069")
            code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
                target_url, expected_country="ph"
            )
            # Final URL is correctly joined: http://127.0.0.1:{port}/ph/app/chatgpt/id6448311069?l=en
            self.assertIn("/ph/", final_url)
            self.assertTrue(final_url.endswith("?l=en"))
            # Document whether is_spoofed is falsely flagged:
            # Under current logic: loc_lower is "?l=en", "/ph/" not in "?l=en" -> is_spoofed is True!
            is_false_positive = is_spoofed
            print(f"\n[EMPIRICAL AUDIT] Location: '?l=en' falsely flagged as spoofed? {is_false_positive}")
        finally:
            q_server.shutdown()


class TestPortNumbersInRedirects(unittest.TestCase):
    """Category 2: Port numbers in redirects."""

    @classmethod
    def setUpClass(cls) -> None:
        class PortHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/redirect-us-port-8443":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com:8443/us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/redirect-ph-port-8443":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com:8443/ph/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/redirect-us-port-8080":
                    self.send_response(302)
                    self.send_header("Location", "http://apps.apple.com:8080/us/app/chatgpt/id6448311069")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = EphemeralRedirectServer(PortHandler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_https_port_8443_redirect_to_us_detected(self) -> None:
        """Assert redirect to https://apps.apple.com:8443/us/ flags is_spoofed=True."""
        url = self.server.get_url("/redirect-us-port-8443")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertEqual(final_url, "https://apps.apple.com:8443/us/app/chatgpt/id6448311069")

    def test_https_port_8443_redirect_to_ph_not_spoofed(self) -> None:
        """Assert in-country redirect to https://apps.apple.com:8443/ph/ is is_spoofed=False."""
        url = self.server.get_url("/redirect-ph-port-8443")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)
        self.assertEqual(final_url, "https://apps.apple.com:8443/ph/app/chatgpt/id6448311069")

    def test_http_port_8080_redirect_to_us_detected(self) -> None:
        """Assert redirect to http://apps.apple.com:8080/us/ flags is_spoofed=True."""
        url = self.server.get_url("/redirect-us-port-8080")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 302)
        self.assertTrue(is_spoofed)
        self.assertEqual(final_url, "http://apps.apple.com:8080/us/app/chatgpt/id6448311069")


class TestProtocolAndSchemeChanges(unittest.TestCase):
    """Category 3: Protocol changes (HTTP -> HTTPS, HTTPS -> HTTP)."""

    @classmethod
    def setUpClass(cls) -> None:
        class SchemeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/upgrade-scheme-ph":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/ph/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/upgrade-scheme-spoof-us":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/downgrade-scheme-spoof-us":
                    self.send_response(302)
                    self.send_header("Location", "http://apps.apple.com/us/app/chatgpt/id6448311069")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = EphemeralRedirectServer(SchemeHandler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_http_to_https_scheme_upgrade_in_country_not_spoofed(self) -> None:
        """Assert legitimate HTTP to HTTPS upgrade within /ph/ is not flagged as spoofed."""
        url = self.server.get_url("/upgrade-scheme-ph")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)
        self.assertEqual(final_url, "https://apps.apple.com/ph/app/chatgpt/id6448311069")

    def test_http_to_https_scheme_upgrade_with_us_fallback_detected(self) -> None:
        """Assert HTTP to HTTPS redirect that falls back to /us/ is flagged as is_spoofed=True."""
        url = self.server.get_url("/upgrade-scheme-spoof-us")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertEqual(final_url, "https://apps.apple.com/us/app/chatgpt/id6448311069")

    def test_https_to_http_downgrade_to_us_detected(self) -> None:
        """Assert redirect from HTTPS to HTTP /us/ is detected as is_spoofed=True."""
        url = self.server.get_url("/downgrade-scheme-spoof-us")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 302)
        self.assertTrue(is_spoofed)


class TestCaseSensitivityStress(unittest.TestCase):
    """Category 4: Case sensitivity variations in country code and Location headers."""

    @classmethod
    def setUpClass(cls) -> None:
        class CaseHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/upper-us":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/US/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/mixed-us":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/Us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/upper-ph":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/PH/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/mixed-ph":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/Ph/app/chatgpt/id6448311069")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = EphemeralRedirectServer(CaseHandler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_uppercase_us_in_location_detected(self) -> None:
        """Assert Location: .../US/... is detected as is_spoofed=True across casing."""
        for expected in ["ph", "PH", "Ph", "  pH  "]:
            with self.subTest(expected_country=expected):
                url = self.server.get_url("/upper-us")
                code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
                    url, expected_country=expected
                )
                self.assertEqual(code, 301)
                self.assertTrue(is_spoofed, f"Failed detecting /US/ spoof for expected '{expected}'")

    def test_mixed_case_us_in_location_detected(self) -> None:
        """Assert Location: .../Us/... is detected as is_spoofed=True across casing."""
        for expected in ["ph", "PH", "Ph"]:
            with self.subTest(expected_country=expected):
                url = self.server.get_url("/mixed-us")
                code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
                    url, expected_country=expected
                )
                self.assertEqual(code, 301)
                self.assertTrue(is_spoofed, f"Failed detecting /Us/ spoof for expected '{expected}'")

    def test_uppercase_ph_in_location_not_spoofed(self) -> None:
        """Assert Location: .../PH/... does not false-positive across expected_country casings."""
        for expected in ["ph", "PH", "Ph", "  pH  "]:
            with self.subTest(expected_country=expected):
                url = self.server.get_url("/upper-ph")
                code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
                    url, expected_country=expected
                )
                self.assertEqual(code, 301)
                self.assertFalse(is_spoofed, f"False positive on /PH/ for expected '{expected}'")

    def test_mixed_case_ph_in_location_not_spoofed(self) -> None:
        """Assert Location: .../Ph/... does not false-positive across expected_country casings."""
        for expected in ["ph", "PH", "Ph"]:
            with self.subTest(expected_country=expected):
                url = self.server.get_url("/mixed-ph")
                code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
                    url, expected_country=expected
                )
                self.assertEqual(code, 301)
                self.assertFalse(is_spoofed, f"False positive on /Ph/ for expected '{expected}'")


class TestThirdCountryRedirects(unittest.TestCase):
    """Category 5 & 6: In-country canonical redirects vs Third-country cross-redirects."""

    @classmethod
    def setUpClass(cls) -> None:
        class CountryHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/ph-to-gb":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/gb/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/ca-to-au":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/au/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/de-to-at":
                    self.send_response(302)
                    self.send_header("Location", "https://apps.apple.com/at/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/in-country-slug-canonical":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/ph/app/chatgpt-official/id6448311069")
                    self.end_headers()
                elif p == "/in-country-trailing-slash":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/ph/app/chatgpt/id6448311069/")
                    self.end_headers()
                elif p == "/us-to-gb":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/gb/app/chatgpt/id6448311069")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = EphemeralRedirectServer(CountryHandler)
        cls.client = HttpClient(timeout=2.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_ph_to_gb_third_country_redirect_detected(self) -> None:
        """Assert requesting PH redirected to Great Britain (GB) is flagged as is_spoofed=True."""
        url = self.server.get_url("/ph-to-gb")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertIn("/gb/", final_url)

    def test_ca_to_au_third_country_redirect_detected(self) -> None:
        """Assert requesting Canada (CA) redirected to Australia (AU) is flagged as is_spoofed=True."""
        url = self.server.get_url("/ca-to-au")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ca"
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)
        self.assertIn("/au/", final_url)

    def test_de_to_at_third_country_redirect_detected(self) -> None:
        """Assert requesting Germany (DE) redirected to Austria (AT) is flagged as is_spoofed=True."""
        url = self.server.get_url("/de-to-at")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="de"
        )
        self.assertEqual(code, 302)
        self.assertTrue(is_spoofed)
        self.assertIn("/at/", final_url)

    def test_in_country_canonical_slug_not_spoofed(self) -> None:
        """Assert in-country slug normalization within /ph/ is is_spoofed=False."""
        url = self.server.get_url("/in-country-slug-canonical")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)
        self.assertIn("/ph/", final_url)

    def test_in_country_trailing_slash_not_spoofed(self) -> None:
        """Assert in-country trailing slash canonicalization within /ph/ is is_spoofed=False."""
        url = self.server.get_url("/in-country-trailing-slash")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="ph"
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)
        self.assertTrue(final_url.endswith("/"))

    def test_us_to_gb_redirect_audited(self) -> None:
        """Empirical probe: Requesting country 'us' redirected to 'gb'.

        Tests whether requesting 'us' and redirecting to 'gb' triggers is_spoofed=True
        or is bypassed due to `if exp != 'us':` in client.py.
        """
        url = self.server.get_url("/us-to-gb")
        code, final_url, body, is_spoofed = self.client.fetch_with_redirect_check(
            url, expected_country="us"
        )
        self.assertEqual(code, 301)
        # Because client.py line 187 checks `if exp != "us":`, when exp == "us" spoof checking is skipped!
        # Document empirical behavior:
        print(f"\n[EMPIRICAL AUDIT] Requesting 'us' redirected to 'gb': is_spoofed={is_spoofed}")


class TestEvasionAttacksAndSlugCollisions(unittest.TestCase):
    """Category 7: Adversarial attacks attempting to evade anti-spoofing detection."""

    def test_app_slug_ph_collision_in_third_country_redirect(self) -> None:
        """Adversarial scenario: App slug is 'ph', and URL redirects to Great Britain (/gb/).

        Location: https://apps.apple.com/gb/app/ph/id6448311069
        Expected country: 'ph'.
        Test whether `/{exp}/` in path matches the slug `/ph/` instead of country prefix,
        causing a spoof evasion bypass.
        """
        class SlugCollisionHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(301)
                self.send_header("Location", "https://apps.apple.com/gb/app/ph/id6448311069")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = EphemeralRedirectServer(SlugCollisionHandler)
        try:
            client = HttpClient()
            url = server.get_url("/target")
            code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
                url, expected_country="ph"
            )
            print(f"\n[EMPIRICAL AUDIT] Slug 'ph' collision bypassed spoof detection? {not is_spoofed}")
            # In current implementation, "/ph/" in "/gb/app/ph/id6448311069" is True!
            # So is_spoofed evaluates to False despite redirecting to Great Britain.
        finally:
            server.shutdown()

    def test_app_slug_us_collision_in_legitimate_storefront(self) -> None:
        """Adversarial scenario: App slug contains 'us' (e.g. app/us/id9999).

        Storefront is legitimately in Philippines: https://apps.apple.com/ph/app/us/id9999
        Redirect: https://apps.apple.com/ph/app/us/id9999/ (canonical trailing slash).
        Test whether `"/us/" in loc_lower` triggers a false positive spoof alarm.
        """
        class SlugUsCollisionHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(301)
                self.send_header("Location", "https://apps.apple.com/ph/app/us/id9999/")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = EphemeralRedirectServer(SlugUsCollisionHandler)
        try:
            client = HttpClient()
            url = server.get_url("/target")
            code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
                url, expected_country="ph"
            )
            print(f"\n[EMPIRICAL AUDIT] App slug '/us/' triggered false-positive spoof alarm? {is_spoofed}")
            # In current implementation, "/us/" in "https://apps.apple.com/ph/app/us/id9999/" is True!
            # So is_spoofed evaluates to True even though country is still ph!
        finally:
            server.shutdown()


class TestAppleValidationResultAndParityAssertions(unittest.TestCase):
    """Category 8: Invariants on AppleValidationResult and cross_validate_pricing."""

    def test_spoofed_validation_result_guarantees_is_valid_false(self) -> None:
        """Assert that whenever is_spoofed=True, is_valid is strictly False and error is populated."""
        class MockSpoofClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 301, "https://apps.apple.com/us/app/chatgpt/id6448311069", b"", True

        validator = AppleStorefrontValidator(client=MockSpoofClient())
        result = validator.validate_product("chatgpt", "xy")

        self.assertTrue(result.is_spoofed, "Result must flag is_spoofed=True")
        self.assertFalse(result.is_valid, "Result must set is_valid=False on spoofing")
        self.assertEqual(result.status_code, 301)
        self.assertIsNotNone(result.error)
        self.assertIn("anti-spoofing triggered", result.error.lower())
        self.assertEqual(result.items, [])
        self.assertEqual(result.iaps, [])

    def test_spoofed_storefront_fails_cross_validate_pricing(self) -> None:
        """Assert cross_validate_pricing fails with anti-spoofing alert when spoofed."""
        product = ProductSummary("ChatGPT", "chatgpt", "/chatgpt", "AI", "https://opentherank.com/chatgpt")
        record = RegionPricingRecord(
            country_code="XY",
            region_name="Unknown Region",
            platform="appstore",
            primary_tier=PriceTier("ChatGPT Plus", "$19.99", 19.99),
        )
        pricing_result = ProductPricingResult(product=product, records=[record])

        class MockSpoofClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 301, "https://apps.apple.com/us/app/chatgpt/id6448311069", b"", True

        validator = AppleStorefrontValidator(client=MockSpoofClient())
        is_valid, err = validator.cross_validate_pricing(pricing_result, "XY")

        self.assertFalse(is_valid)
        self.assertIsNotNone(err)
        self.assertIn("Anti-spoofing alert", err)
        self.assertFalse(pricing_result.first_party_validated)
        self.assertTrue(any("Anti-spoofing alert" in n for n in pricing_result.validation_notes))

    def test_in_country_301_without_body_invariants(self) -> None:
        """Assert behavior of an in-country 301 canonical redirect.

        Evaluates whether an in-country 301 returning empty/redirect body
        marks is_valid=True with 0 items, or how the system treats it.
        """
        class InCountry301MockClient:
            def fetch_with_redirect_check(self, url, expected_country=None):
                return 301, "https://apps.apple.com/ph/app/chatgpt-canonical/id6448311069", b"<html>301 Redirect</html>", False

        validator = AppleStorefrontValidator(client=InCountry301MockClient())
        result = validator.validate_product("chatgpt", "ph")

        self.assertFalse(result.is_spoofed)
        self.assertEqual(result.status_code, 301)
        print(f"\n[EMPIRICAL AUDIT] In-country 301 without items: is_valid={result.is_valid}, items_len={len(result.items)}")


if __name__ == "__main__":
    unittest.main()
