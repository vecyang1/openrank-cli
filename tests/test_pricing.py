"""Unit test suite for openrank.pricing (PricingEngine, SSR HTML parser, and arbitrage)."""
import os
import unittest
from pathlib import Path
from typing import Dict, List, Optional

from openrank.models import PriceTier, ProductPricingResult, ProductSummary, RegionPricingRecord
from openrank.pricing import (
    HTMLNode,
    HTMLTreeParser,
    PricingEngine,
    PricingExtractor,
    fetch_pricing,
    parse_pricing_html,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class MockHttpClient:
    """Mock HTTP client returning canned fixture bytes."""

    def __init__(self, fixture_map: Dict[str, bytes]) -> None:
        self.fixture_map = fixture_map
        self.calls: List[str] = []

    def fetch_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.fixture_map:
            return self.fixture_map[url]
        raise ValueError(f"Unexpected mock URL: {url}")

    def get(self, url: str) -> tuple:
        body = self.fetch_bytes(url)
        return 200, {}, body


class TestPricingEngine(unittest.TestCase):
    """Comprehensive test suite for PricingEngine and SSR HTML table parsing."""

    def setUp(self) -> None:
        self.engine = PricingEngine()
        self.chatgpt_html = (FIXTURES_DIR / "chatgpt_pricing.html").read_text(encoding="utf-8")
        self.netflix_html = (FIXTURES_DIR / "netflix_pricing.html").read_text(encoding="utf-8")
        self.iphone_html = (FIXTURES_DIR / "iphone_pricing.html").read_text(encoding="utf-8")

        self.chatgpt_prod = ProductSummary(
            name="ChatGPT",
            slug="chatgpt",
            path="/ai-pricing/chatgpt/",
            category="AI Subscriptions",
            url="https://opentherank.com/ai-pricing/chatgpt/",
        )
        self.netflix_prod = ProductSummary(
            name="Netflix",
            slug="netflix",
            path="/streaming-pricing/netflix/",
            category="Streaming",
            url="https://opentherank.com/streaming-pricing/netflix/",
        )
        self.iphone_prod = ProductSummary(
            name="iPhone 17 Pro",
            slug="iphone-17-pro",
            path="/apple-pricing/iphone-17-pro/",
            category="Apple Hardware",
            url="https://opentherank.com/apple-pricing/iphone-17-pro/",
        )

    def test_chatgpt_multi_platform_extraction(self) -> None:
        """Assert both App Store and Google Play tables are extracted with multi-tier prices."""
        result = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="all")

        self.assertIsInstance(result, ProductPricingResult)
        self.assertEqual(result.product.name, "ChatGPT")
        self.assertEqual(result.us_baseline_usd, 19.99)
        self.assertEqual(result.baseline_us_price, 19.99)
        self.assertIn("appstore", result.platforms)
        self.assertIn("play", result.platforms)

        # 7 appstore records + 4 play records = 11 records
        self.assertEqual(len(result.records), 11)

        # Verify Philippines App Store record
        ph_appstore = next(r for r in result.records if r.country_code == "PH" and r.platform == "appstore")
        self.assertEqual(ph_appstore.region_name, "Philippines")
        self.assertEqual(ph_appstore.primary_tier.name, "Plus")
        self.assertEqual(ph_appstore.primary_tier.local_price_str, "≈ ₱999/mo")
        self.assertAlmostEqual(ph_appstore.primary_tier.usd_price, 15.89, places=2)
        self.assertEqual(ph_appstore.vs_us_delta, -21.0)
        self.assertEqual(ph_appstore.tax_info, "12% VAT included")
        self.assertEqual(ph_appstore.risk_level, "Medium")
        self.assertIn("billing address", ph_appstore.risk_note)

        # Check additional tiers on Philippines record
        self.assertIn("Pro 5x", ph_appstore.additional_tiers)
        self.assertIn("Pro 20x", ph_appstore.additional_tiers)
        self.assertIn("Pro 500", ph_appstore.additional_tiers)
        pro_5x = ph_appstore.additional_tiers["Pro 5x"]
        self.assertEqual(pro_5x.name, "Pro 5x")
        self.assertEqual(pro_5x.local_price_str, "≈ ₱6,490/mo")
        self.assertAlmostEqual(pro_5x.usd_price, 103.22, places=2)

        # Verify Google Play Turkey record
        tr_play = next(r for r in result.records if r.country_code == "TR" and r.platform == "play")
        self.assertEqual(tr_play.region_name, "Turkey")
        self.assertEqual(tr_play.risk_level, "High")
        self.assertIn("Google Play", tr_play.risk_note)
        self.assertEqual(tr_play.vs_us_delta, -9.0)

    def test_netflix_streaming_tiers_extraction(self) -> None:
        """Assert Netflix streaming subscription tiers (Basic, Standard, Premium) are parsed."""
        result = self.engine.parse_pricing_html(self.netflix_html, self.netflix_prod)

        self.assertEqual(result.us_baseline_usd, 9.99)
        self.assertEqual(len(result.records), 5)

        # Check Brazil record
        br = next(r for r in result.records if r.country_code == "BR")
        self.assertEqual(br.region_name, "Brazil")
        self.assertEqual(br.primary_tier.name, "Basic")
        self.assertEqual(br.primary_tier.local_price_str, "R$ 20.90/mo")
        self.assertAlmostEqual(br.primary_tier.usd_price, 3.80, places=2)
        self.assertEqual(br.vs_us_delta, -62.0)
        self.assertEqual(br.risk_level, "Medium")
        self.assertIn("CPF", br.risk_note)

        # Additional tiers on Brazil
        self.assertIn("Standard", br.additional_tiers)
        self.assertIn("Premium", br.additional_tiers)
        self.assertEqual(br.additional_tiers["Standard"].local_price_str, "R$ 44.90/mo")
        self.assertAlmostEqual(br.additional_tiers["Standard"].usd_price, 8.16, places=2)
        self.assertEqual(br.additional_tiers["Premium"].local_price_str, "R$ 59.90/mo")
        self.assertAlmostEqual(br.additional_tiers["Premium"].usd_price, 10.89, places=2)

        # Check Turkey high risk SMS verification
        tr = next(r for r in result.records if r.country_code == "TR")
        self.assertEqual(tr.risk_level, "High")
        self.assertIn("SMS verification", tr.risk_note)

    def test_iphone_hardware_storage_tiers(self) -> None:
        """Assert Apple hardware storage tiers (256GB, 512GB, 1TB) are parsed with deltas."""
        result = self.engine.parse_pricing_html(self.iphone_html, self.iphone_prod)

        self.assertEqual(result.us_baseline_usd, 1099.00)
        self.assertEqual(len(result.records), 6)

        # US baseline
        us = next(r for r in result.records if r.country_code == "US")
        self.assertEqual(us.primary_tier.name, "256GB")
        self.assertEqual(us.primary_tier.usd_price, 1099.00)
        self.assertEqual(us.vs_us_delta, 0.0)
        self.assertIn("512GB", us.additional_tiers)
        self.assertEqual(us.additional_tiers["512GB"].usd_price, 1299.00)
        self.assertIn("1TB", us.additional_tiers)
        self.assertEqual(us.additional_tiers["1TB"].usd_price, 1499.00)

        # Turkey huge tariff markup
        tr = next(r for r in result.records if r.country_code == "TR")
        self.assertEqual(tr.vs_us_delta, 129.0)
        self.assertEqual(tr.primary_tier.local_price_str, "₺89,999")
        self.assertAlmostEqual(tr.primary_tier.usd_price, 2513.93, places=2)
        self.assertIn("IMEI", tr.risk_note)

        # Brazil import duties markup
        br = next(r for r in result.records if r.country_code == "BR")
        self.assertEqual(br.vs_us_delta, 90.0)

    def test_platform_filter_isolation(self) -> None:
        """Assert --platform {all,appstore,play} isolates or combines store-specific tables."""
        res_all = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="all")
        self.assertEqual(len(res_all.records), 11)

        res_appstore = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="appstore")
        self.assertEqual(len(res_appstore.records), 7)
        self.assertTrue(all(r.platform == "appstore" for r in res_appstore.records))

        res_play = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="play")
        self.assertEqual(len(res_play.records), 4)
        self.assertTrue(all(r.platform == "play" for r in res_play.records))

        # Unknown platform returns empty list cleanly
        res_none = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="nintendo")
        self.assertEqual(len(res_none.records), 0)

    def test_country_filter_exact_match(self) -> None:
        """Assert --country <cc> strictly matches 2-letter ISO code without substring leakage."""
        # 1. Matching 'PH'
        res_ph = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, country="PH")
        self.assertEqual(len(res_ph.records), 2)  # App Store and Google Play
        self.assertTrue(all(r.country_code == "PH" for r in res_ph.records))

        # 2. Case-insensitivity and whitespace padding
        res_ph_lower = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, country="  ph  ")
        self.assertEqual(len(res_ph_lower.records), 2)

        # 3. CRITICAL: 'IN' matches India and does NOT match Finland ('FI')
        res_in = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, country="IN")
        self.assertEqual(len(res_in.records), 1)
        self.assertEqual(res_in.records[0].country_code, "IN")
        self.assertEqual(res_in.records[0].region_name, "India")
        for r in res_in.records:
            self.assertNotEqual(r.country_code, "FI")
            self.assertNotEqual(r.region_name, "Finland")

        # 4. 'FI' matches Finland
        res_fi = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, country="FI")
        self.assertEqual(len(res_fi.records), 1)
        self.assertEqual(res_fi.records[0].country_code, "FI")
        self.assertEqual(res_fi.records[0].region_name, "Finland")

        # 5. Non-existent country returns empty records without crash
        res_xx = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, country="XX")
        self.assertEqual(len(res_xx.records), 0)

    def test_combined_platform_and_country_filters(self) -> None:
        """Assert combining --platform and --country filters isolates exactly one record."""
        res = self.engine.parse_pricing_html(
            self.chatgpt_html,
            self.chatgpt_prod,
            platform="appstore",
            country="PH",
        )
        self.assertEqual(len(res.records), 1)
        rec = res.records[0]
        self.assertEqual(rec.platform, "appstore")
        self.assertEqual(rec.country_code, "PH")
        self.assertEqual(rec.region_name, "Philippines")

    def test_dynamic_delta_calculation(self) -> None:
        """Assert deltas are dynamically calculated when HTML lacks explicit ctl-delta."""
        html_without_delta = """
        <div class="product-header" data-us-baseline="20.00">
          <h1>Test Product</h1>
        </div>
        <table id="test-table" data-platform="web">
          <thead><tr><th>CC</th><th>Region</th><th>Tier</th></tr></thead>
          <tbody>
            <tr data-cc="US">
              <td>US</td><td>United States</td>
              <td data-plan="Basic" data-otr-usd="20.00">$20.00</td>
            </tr>
            <tr data-cc="CA">
              <td>CA</td><td>Canada</td>
              <td data-plan="Basic" data-otr-usd="16.00">$16.00</td>
            </tr>
            <tr data-cc="GB">
              <td>GB</td><td>United Kingdom</td>
              <td data-plan="Basic" data-otr-usd="25.00">$25.00</td>
            </tr>
          </tbody>
        </table>
        """
        res = self.engine.parse_pricing_html(html_without_delta, self.chatgpt_prod)
        self.assertEqual(res.baseline_us_price, 20.00)
        self.assertEqual(len(res.records), 3)

        us = next(r for r in res.records if r.country_code == "US")
        self.assertEqual(us.vs_us_delta, 0.0)

        ca = next(r for r in res.records if r.country_code == "CA")
        # (16 - 20) / 20 * 100 = -20.0%
        self.assertEqual(ca.vs_us_delta, -20.0)
        self.assertEqual(ca.primary_tier.delta_percent, -20.0)

        gb = next(r for r in res.records if r.country_code == "GB")
        # (25 - 20) / 20 * 100 = +25.0%
        self.assertEqual(gb.vs_us_delta, 25.0)

    def test_empty_and_corrupt_html_graceful_handling(self) -> None:
        """Assert empty, None, and corrupted HTML strings never crash and return empty results."""
        # Empty string
        res_empty = self.engine.parse_pricing_html("", self.chatgpt_prod)
        self.assertEqual(len(res_empty.records), 0)
        self.assertIsNone(res_empty.baseline_us_price)

        # Whitespace only
        res_ws = self.engine.parse_pricing_html("   \n\t  ", self.chatgpt_prod)
        self.assertEqual(len(res_ws.records), 0)

        # Non-table HTML
        res_notable = self.engine.parse_pricing_html(
            "<html><body><div><h1>404 Not Found</h1></div></body></html>",
            self.chatgpt_prod,
        )
        self.assertEqual(len(res_notable.records), 0)

        # Table without rows
        res_norows = self.engine.parse_pricing_html(
            "<table><thead><tr><th>Rank</th></tr></thead><tbody></tbody></table>",
            self.chatgpt_prod,
        )
        self.assertEqual(len(res_norows.records), 0)

        # Corrupted unclosed HTML tags
        malformed_html = "<table><tr><td><use href='#f-PH'><td><span class='ctl-region-link'>PH<td data-otr-usd='10'>$10"
        res_malformed = self.engine.parse_pricing_html(malformed_html, self.chatgpt_prod)
        self.assertIsInstance(res_malformed, ProductPricingResult)

    def test_fetch_pricing_integration(self) -> None:
        """Assert fetch_pricing connects to client and parses HTML accurately."""
        mock_client = MockHttpClient({
            "https://opentherank.com/ai-pricing/chatgpt/": self.chatgpt_html.encode("utf-8")
        })
        engine = PricingEngine(client=mock_client)
        result = engine.fetch_pricing(self.chatgpt_prod, platform="appstore", country="PH")

        self.assertEqual(len(mock_client.calls), 1)
        self.assertEqual(mock_client.calls[0], "https://opentherank.com/ai-pricing/chatgpt/")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].country_code, "PH")
        self.assertEqual(result.records[0].platform, "appstore")

    def test_module_level_helpers_and_aliases(self) -> None:
        """Assert module-level functions and PricingExtractor alias work identically."""
        # PricingExtractor alias
        extractor = PricingExtractor()
        res1 = extractor.parse_pricing_html(self.netflix_html, self.netflix_prod)
        self.assertEqual(len(res1.records), 5)

        # Module-level parse_pricing_html
        res2 = parse_pricing_html(self.netflix_html, self.netflix_prod, country="TR")
        self.assertEqual(len(res2.records), 1)
        self.assertEqual(res2.records[0].country_code, "TR")

        # parse_html_tables convenience
        records = extractor.parse_html_tables(self.netflix_html, self.netflix_prod)
        self.assertEqual(len(records), 5)

    def test_to_dict_and_json_serialization(self) -> None:
        """Assert ProductPricingResult and RegionPricingRecord serialize cleanly to dict."""
        result = self.engine.parse_pricing_html(self.chatgpt_html, self.chatgpt_prod, platform="appstore", country="PH")
        d = result.to_dict()

        self.assertIn("product", d)
        self.assertIn("records", d)
        self.assertIn("baseline_us_price", d)
        self.assertEqual(d["product"]["name"], "ChatGPT")
        self.assertEqual(len(d["records"]), 1)

        rec_dict = d["records"][0]
        self.assertEqual(rec_dict["country_code"], "PH")
        self.assertEqual(rec_dict["primary_tier"]["name"], "Plus")
        self.assertEqual(rec_dict["primary_tier"]["usd_price"], 15.89)
        self.assertIn("Pro 5x", rec_dict["additional_tiers"])

    def test_html_entity_decoding(self) -> None:
        """Assert HTML entities like &pound; or &#8369; are properly unescaped in prices."""
        html_entities = """
        <table>
          <thead><tr><th>CC</th><th>Region</th><th>Plan</th><th>Price</th></tr></thead>
          <tbody>
            <tr data-cc="GB">
              <td>GB</td><td>United Kingdom</td>
              <td data-plan="Plus" data-otr-usd="25.00">&pound;19.99/mo</td>
            </tr>
            <tr data-cc="PH">
              <td>PH</td><td>Philippines</td>
              <td data-plan="Plus" data-otr-usd="15.89">&#8369;999/mo</td>
            </tr>
          </tbody>
        </table>
        """
        res = self.engine.parse_pricing_html(html_entities, self.chatgpt_prod)
        self.assertEqual(len(res.records), 2)
        gb = next(r for r in res.records if r.country_code == "GB")
        self.assertEqual(gb.primary_tier.local_price_str, "£19.99/mo")
        ph = next(r for r in res.records if r.country_code == "PH")
        self.assertEqual(ph.primary_tier.local_price_str, "₱999/mo")

    def test_flag_class_and_lowercase_cc(self) -> None:
        """Assert country codes from flag classes or lowercase attributes are extracted uppercase."""
        html_flags = """
        <table>
          <thead><tr><th>Flag</th><th>Region</th><th>Price</th></tr></thead>
          <tbody>
            <tr>
              <td><span class="flag-jp"></span></td>
              <td><span class="ctl-region-link">Japan</span></td>
              <td data-plan="Pro" data-otr-usd="20.00">&yen;3,000</td>
            </tr>
            <tr data-cc="ca">
              <td></td>
              <td>Canada</td>
              <td data-plan="Pro" data-otr-usd="18.00">CA$25.00</td>
            </tr>
          </tbody>
        </table>
        """
        res = self.engine.parse_pricing_html(html_flags, self.chatgpt_prod)
        self.assertEqual(len(res.records), 2)
        jp = next(r for r in res.records if r.country_code == "JP")
        self.assertEqual(jp.country_code, "JP")
        self.assertEqual(jp.region_name, "Japan")
        ca = next(r for r in res.records if r.country_code == "CA")
        self.assertEqual(ca.country_code, "CA")

    def test_table_index_platform_fallback(self) -> None:
        """Assert untagged tables default to Table 0=appstore, Table 1=play, Table 2=web."""
        multi_table_html = """
        <table>
          <thead><tr><th>CC</th><th>Price</th></tr></thead>
          <tbody><tr data-cc="US"><td>US</td><td data-otr-usd="10">$10</td></tr></tbody>
        </table>
        <table>
          <thead><tr><th>CC</th><th>Price</th></tr></thead>
          <tbody><tr data-cc="US"><td>US</td><td data-otr-usd="10">$10</td></tr></tbody>
        </table>
        <table>
          <thead><tr><th>CC</th><th>Price</th></tr></thead>
          <tbody><tr data-cc="US"><td>US</td><td data-otr-usd="10">$10</td></tr></tbody>
        </table>
        """
        res = self.engine.parse_pricing_html(multi_table_html, self.chatgpt_prod, platform="all")
        self.assertEqual(len(res.records), 3)
        self.assertEqual(res.records[0].platform, "appstore")
        self.assertEqual(res.records[1].platform, "play")
        self.assertEqual(res.records[2].platform, "web")


if __name__ == "__main__":
    unittest.main()

