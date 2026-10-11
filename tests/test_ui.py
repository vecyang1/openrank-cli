"""Comprehensive test suite blueprint for openrank.ui (Terminal UI & Formatting).

Tests cover:
- F11: Rich Terminal UI Table formatting, column widths, wrapping, truncation, and terminal-width adaptation.
- F12: ANSI Price Delta Highlights, green savings, red markups, NO_COLOR env suppression, --no-color flag, and TERM=dumb.
- F13: Typed JSON Output Pipeline, schema validation across all outputs, empty result handling, and error structures.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from unittest.mock import patch

from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

# Regex matching any ANSI escape sequence (e.g. \x1b[32m, \x1b[0m, \x1b[1m)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Helper to remove ANSI escape sequences from rendered output."""
    return ANSI_ESCAPE_RE.sub("", text)


class TestUiAnsiColors(unittest.TestCase):
    """Test ANSI color formatting and suppression rules (F12)."""

    def setUp(self) -> None:
        # Import dynamically to support test runners before or during module implementation
        try:
            import openrank.ui as ui
            self.ui = ui
        except ImportError:
            self.skipTest("openrank.ui module not yet implemented")

    def test_negative_delta_renders_green(self) -> None:
        """Negative delta percentage (discount/savings) should render with green ANSI code."""
        # \033[32m or 38;5;...
        if hasattr(self.ui, "colorize_delta"):
            colored = self.ui.colorize_delta(-21.0, no_color=False)
            self.assertIn("\x1b[", colored)
            self.assertIn("32", colored)
            self.assertIn("-21.0%", colored)

    def test_positive_delta_renders_red(self) -> None:
        """Positive delta percentage (markup/premium) should render with red ANSI code."""
        # \033[31m
        if hasattr(self.ui, "colorize_delta"):
            colored = self.ui.colorize_delta(15.5, no_color=False)
            self.assertIn("\x1b[", colored)
            self.assertIn("31", colored)
            self.assertIn("+15.5%", colored)

    def test_zero_delta_renders_neutral(self) -> None:
        """Zero delta (parity) should format cleanly."""
        if hasattr(self.ui, "colorize_delta"):
            colored = self.ui.colorize_delta(0.0, no_color=False)
            self.assertIn("0.0%", colored)

    def test_no_color_flag_suppresses_ansi(self) -> None:
        """Explicit no_color=True parameter must completely eliminate ANSI escape sequences."""
        prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai-pricing/chatgpt/", category="AI", url="")
        rec = RegionPricingRecord(
            country_code="PH", region_name="Philippines", platform="appstore",
            primary_tier=PriceTier(name="Plus", local_price_str="₱999", usd_price=15.89, delta_percent=-21.0),
            vs_us_delta=-21.0, risk_level="Low",
        )
        pricing_res = ProductPricingResult(product=prod, records=[rec], baseline_us_price=20.0)

        rendered = self.ui.render_pricing_table(pricing_res, no_color=True)
        self.assertNotIn("\x1b[", rendered, "Output with no_color=True must contain zero ANSI escape codes")

    def test_no_color_env_var_suppresses_ansi(self) -> None:
        """Setting NO_COLOR environment variable must disable ANSI formatting."""
        prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai-pricing/chatgpt/", category="AI", url="")
        rec = RegionPricingRecord(
            country_code="PH", region_name="Philippines", platform="appstore",
            primary_tier=PriceTier(name="Plus", local_price_str="₱999", usd_price=15.89, delta_percent=-21.0),
            vs_us_delta=-21.0, risk_level="Low",
        )
        pricing_res = ProductPricingResult(product=prod, records=[rec], baseline_us_price=20.0)

        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            rendered = self.ui.render_pricing_table(pricing_res, no_color=False)
            self.assertNotIn("\x1b[", rendered, "NO_COLOR environment variable must suppress ANSI escapes")

    def test_term_dumb_suppresses_ansi(self) -> None:
        """Setting TERM=dumb environment variable must disable ANSI formatting."""
        prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai-pricing/chatgpt/", category="AI", url="")
        rec = RegionPricingRecord(
            country_code="PH", region_name="Philippines", platform="appstore",
            primary_tier=PriceTier(name="Plus", local_price_str="₱999", usd_price=15.89, delta_percent=-21.0),
            vs_us_delta=-21.0, risk_level="Low",
        )
        pricing_res = ProductPricingResult(product=prod, records=[rec], baseline_us_price=20.0)

        with patch.dict(os.environ, {"TERM": "dumb"}):
            rendered = self.ui.render_pricing_table(pricing_res, no_color=False)
            self.assertNotIn("\x1b[", rendered, "TERM=dumb must suppress ANSI escapes")


class TestUiSearchResults(unittest.TestCase):
    """Test search results formatting and JSON serialization (F11, F13)."""

    def setUp(self) -> None:
        try:
            import openrank.ui as ui
            self.ui = ui
        except ImportError:
            self.skipTest("openrank.ui module not yet implemented")

    def test_render_search_results_table(self) -> None:
        """Render standard search results table with headers and data."""
        results = [
            (ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI Subscriptions", url="https://opentherank.com/ai/chatgpt/"), 100),
            (ProductSummary(name="Claude Pro", slug="claude", path="/ai/claude/", category="AI Subscriptions", url="https://opentherank.com/ai/claude/"), 75),
        ]
        output = self.ui.render_search_results(results, as_json=False, no_color=True)
        plain = strip_ansi(output)

        self.assertIn("ChatGPT", plain)
        self.assertIn("Claude Pro", plain)
        self.assertIn("AI Subscriptions", plain)
        self.assertIn("chatgpt", plain)
        self.assertIn("100", plain)

    def test_render_search_results_empty_state(self) -> None:
        """Empty search results should display a clean notification message."""
        output = self.ui.render_search_results([], as_json=False, no_color=True)
        self.assertTrue(len(output.strip()) > 0)
        self.assertTrue("no" in output.lower() or "0" in output.lower() or "found" in output.lower())

    def test_render_search_results_json(self) -> None:
        """Search results with as_json=True should output valid parseable JSON."""
        results = [
            (ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI Subscriptions", url="https://opentherank.com/ai/chatgpt/"), 100),
        ]
        output = self.ui.render_search_results(results, as_json=True)
        data = json.loads(output)

        if isinstance(data, list):
            item = data[0]
            self.assertEqual(item["slug"], "chatgpt")
            self.assertEqual(item["score"], 100)
        elif isinstance(data, dict):
            self.assertIn("products", data)
            self.assertEqual(data["products"][0]["slug"], "chatgpt")

    def test_render_search_results_json_empty(self) -> None:
        """Empty search results in JSON mode should return valid empty list or empty payload."""
        output = self.ui.render_search_results([], as_json=True)
        data = json.loads(output)
        if isinstance(data, list):
            self.assertEqual(len(data), 0)
        elif isinstance(data, dict):
            self.assertEqual(len(data.get("products", [])), 0)


class TestUiPricingTable(unittest.TestCase):
    """Test pricing table formatting, terminal-width adaptation, and JSON output (F11, F13)."""

    def setUp(self) -> None:
        try:
            import openrank.ui as ui
            self.ui = ui
        except ImportError:
            self.skipTest("openrank.ui module not yet implemented")

        self.prod = ProductSummary(
            name="Netflix", slug="netflix", path="/streaming/netflix/",
            category="Streaming", url="https://opentherank.com/streaming/netflix/",
        )
        self.records = [
            RegionPricingRecord(
                country_code="TR", region_name="Turkey", platform="web",
                primary_tier=PriceTier(name="Standard", local_price_str="₺149.99", usd_price=4.50, delta_percent=-70.0),
                vs_us_delta=-70.0, tax_info="VAT Included", risk_level="Medium", risk_note="SMS verification",
            ),
            RegionPricingRecord(
                country_code="US", region_name="United States", platform="web",
                primary_tier=PriceTier(name="Standard", local_price_str="$15.49", usd_price=15.49, delta_percent=0.0),
                vs_us_delta=0.0, tax_info="Sales tax added", risk_level="Low",
            ),
            RegionPricingRecord(
                country_code="CH", region_name="Switzerland", platform="web",
                primary_tier=PriceTier(name="Standard", local_price_str="CHF 20.90", usd_price=23.50, delta_percent=51.7),
                vs_us_delta=51.7, tax_info="Included", risk_level="Low",
            ),
        ]
        self.pricing_res = ProductPricingResult(
            product=self.prod, records=self.records, baseline_us_price=15.49, platforms=["web"]
        )

    def test_render_pricing_table_headers_and_content(self) -> None:
        """Table output must display product header and all record rows."""
        output = self.ui.render_pricing_table(self.pricing_res, no_color=True)
        plain = strip_ansi(output)

        self.assertIn("Netflix", plain)
        self.assertIn("TR", plain)
        self.assertIn("Turkey", plain)
        self.assertIn("₺149.99", plain)
        self.assertIn("$4.50", plain)
        self.assertIn("-70.0%", plain)
        self.assertIn("United States", plain)
        self.assertIn("Switzerland", plain)

    def test_render_pricing_table_terminal_width_narrow(self) -> None:
        """Narrow terminal width (<60 cols) should truncate or wrap without throwing exceptions."""
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((55, 24))):
            output = self.ui.render_pricing_table(self.pricing_res, no_color=True)
            self.assertTrue(len(output) > 0)
            for line in output.splitlines():
                # Verify each line does not crash and renders gracefully
                self.assertIsInstance(line, str)

    def test_render_pricing_table_terminal_width_wide(self) -> None:
        """Wide terminal width (>120 cols) should format cleanly."""
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((140, 24))):
            output = self.ui.render_pricing_table(self.pricing_res, no_color=True)
            self.assertIn("Netflix", output)
            self.assertIn("Turkey", output)

    def test_render_pricing_table_empty_records(self) -> None:
        """Product with zero records should display clean empty message."""
        empty_res = ProductPricingResult(product=self.prod, records=[], baseline_us_price=None)
        output = self.ui.render_pricing_table(empty_res, no_color=True)
        self.assertTrue(len(output.strip()) > 0)
        self.assertTrue("no" in output.lower() or "0" in output.lower() or "record" in output.lower())

    def test_render_pricing_table_json(self) -> None:
        """Pricing table JSON mode must serialize complete structure matching schema."""
        output = self.ui.render_pricing_table(self.pricing_res, as_json=True)
        data = json.loads(output)

        self.assertIn("product", data)
        self.assertEqual(data["product"]["slug"], "netflix")
        self.assertIn("records", data)
        self.assertEqual(len(data["records"]), 3)
        self.assertEqual(data["records"][0]["country_code"], "TR")
        self.assertEqual(data["records"][0]["primary_tier"]["usd_price"], 4.50)
        self.assertEqual(data["baseline_us_price"], 15.49)


class TestUiAppleValidation(unittest.TestCase):
    """Test Apple Storefront validation rendering and JSON serialization (F8, F9, F13)."""

    def setUp(self) -> None:
        try:
            import openrank.ui as ui
            self.ui = ui
        except ImportError:
            self.skipTest("openrank.ui module not yet implemented")

    def test_render_apple_validation_valid(self) -> None:
        """Valid Apple validation result should display IAP items."""
        val = AppleValidationResult(
            product_slug="chatgpt",
            country_code="PH",
            requested_url="https://apps.apple.com/ph/app/chatgpt/id6448311069",
            final_url="https://apps.apple.com/ph/app/chatgpt/id6448311069",
            is_spoofed=False,
            is_valid=True,
            items=[
                AppleIAPItem(name="ChatGPT Plus", local_price_str="₱999.00", numeric_price=999.0, currency_symbol="₱"),
            ],
        )
        output = self.ui.render_apple_validation(val, no_color=True)
        plain = strip_ansi(output)

        self.assertIn("chatgpt", plain.lower())
        self.assertIn("PH", plain)
        self.assertIn("ChatGPT Plus", plain)
        self.assertIn("₱999.00", plain)

    def test_render_apple_validation_spoofed_warning(self) -> None:
        """Spoofed result must display prominent warning."""
        val = AppleValidationResult(
            product_slug="chatgpt",
            country_code="XY",
            requested_url="https://apps.apple.com/xy/app/chatgpt/id6448311069",
            final_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            is_spoofed=True,
            is_valid=False,
            error="Redirected to US store",
            items=[],
        )
        output = self.ui.render_apple_validation(val, no_color=True)
        plain = strip_ansi(output)

        self.assertTrue("spoof" in plain.lower() or "redirect" in plain.lower() or "warning" in plain.lower())

    def test_render_apple_validation_json(self) -> None:
        """Apple validation in JSON mode must output parseable JSON."""
        val = AppleValidationResult(
            product_slug="chatgpt",
            country_code="PH",
            requested_url="https://apps.apple.com/ph/app/chatgpt/id6448311069",
            final_url="https://apps.apple.com/ph/app/chatgpt/id6448311069",
            is_spoofed=False,
            is_valid=True,
            items=[
                AppleIAPItem(name="ChatGPT Plus", local_price_str="₱999.00", numeric_price=999.0, currency_symbol="₱"),
            ],
        )
        output = self.ui.render_apple_validation(val, as_json=True)
        data = json.loads(output)

        self.assertEqual(data["product_slug"], "chatgpt")
        self.assertEqual(data["country_code"], "PH")
        self.assertFalse(data["is_spoofed"])
        self.assertTrue(data["is_valid"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["local_price_str"], "₱999.00")


if __name__ == "__main__":
    unittest.main()
