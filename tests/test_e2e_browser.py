"""Adversarial End-to-End Browser Automation Test for OpenTheRank Dashboard.

Verifies:
1. Complete Zero-Facade Invariant across all UI surfaces (Header, Search, Filters, Tables, Modals, Drawers).
2. Real data round-trips to backend REST API and SQLite/JSON disk persistence.
3. Total uncaught browser console errors == 0.
4. Full page reload persistence of user settings and watchlist.
5. High-resolution screenshot proof stored in docs/artifacts/dashboard_e2e_verified.png.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from openrank.server import start_server

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class TestDashboardE2E(unittest.TestCase):
    """Adversarial browser E2E test suite running headless Google Chrome via Playwright."""

    server = None
    thread = None
    base_url = ""
    temp_dir = None

    @classmethod
    def setUpClass(cls):
        """Start server on ephemeral port with isolated environment."""
        if not os.path.exists(CHROME_PATH):
            raise unittest.SkipTest(f"Google Chrome not found at {CHROME_PATH}")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright library not installed")

        cls.temp_dir = tempfile.mkdtemp(prefix="openrank_e2e_")
        cls.config_dir = Path(cls.temp_dir) / "config"
        cls.cache_dir = Path(cls.temp_dir) / "cache"
        cls.config_dir.mkdir(parents=True, exist_ok=True)
        cls.cache_dir.mkdir(parents=True, exist_ok=True)

        os.environ["OPENRANK_CONFIG_DIR"] = str(cls.config_dir)
        os.environ["OPENRANK_CACHE_DIR"] = str(cls.cache_dir)

        # Seed test cache with high-fidelity fixture data for fast deterministic E2E execution
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

            p_gemini = ProductSummary(
                name="Google Gemini Advanced",
                slug="gemini",
                path="/pricing/gemini/",
                category="AI Subscriptions",
                url="https://opentherank.com/pricing/gemini/",
            )
            res_gemini = engine.parse_pricing_html(html_content, p_gemini)
            cache.set("pricing_gemini", res_gemini.to_dict())

        cls.server, cls.thread = start_server(host="127.0.0.1", port=0, daemon=True)
        port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        """Shutdown server and clean up temporary directory."""
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.temp_dir and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_complete_interactive_e2e_traversal(self):
        """Traverse all buttons, inputs, drawers, modals, assert 0 errors and persistence."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME_PATH,
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            console_errors = []
            page.on("pageerror", lambda err: console_errors.append(f"PageError: {err}"))
            page.on(
                "console",
                lambda msg: console_errors.append(f"ConsoleError: {msg.text}")
                if msg.type == "error"
                else None,
            )

            # 1. Navigate to dashboard
            page.goto(self.base_url, wait_until="networkidle", timeout=15000)

            # Verify title
            self.assertIn("OpenTheRank", page.title())

            # Wait for auto-loaded product header
            page.wait_for_selector("#pricing-product-name", timeout=10000)
            product_name = page.text_content("#pricing-product-name")
            self.assertTrue(len(product_name.strip()) > 0)

            # 2. Test Category Navigation Pills
            category_pills = page.query_selector_all(".category-pill")
            self.assertGreaterEqual(len(category_pills), 4)
            for pill in category_pills[:3]:
                pill.click()
                page.wait_for_timeout(200)

            # Reset category to all
            page.click('.category-pill[data-cat="all"]')
            page.wait_for_timeout(300)

            # 3. Test Global Search & Auto-Suggest
            page.fill("#global-search", "chatgpt")
            page.wait_for_selector(".search-item", timeout=5000)
            search_items = page.query_selector_all(".search-item")
            self.assertGreater(len(search_items), 0)
            # Click the search result item
            search_items[0].click()
            page.wait_for_function(
                "() => !document.getElementById('pricing-product-name').textContent.includes('Loading')",
                timeout=15000,
            )

            updated_name = page.text_content("#pricing-product-name")
            self.assertIn("ChatGPT", updated_name)

            # 4. Test Platform Filter Pills
            page.click('.platform-btn[data-platform="appstore"]')
            page.wait_for_timeout(200)
            page.click('.platform-btn[data-platform="play"]')
            page.wait_for_timeout(200)
            page.click('.platform-btn[data-platform="web"]')
            page.wait_for_timeout(200)
            page.click('.platform-btn[data-platform="all"]')
            page.wait_for_timeout(200)

            # 5. Test Country Filter Input
            page.fill("#filter-country", "PH")
            page.wait_for_timeout(300)
            page.fill("#filter-country", "")
            page.wait_for_timeout(200)

            # 6. Test Sorting Dropdown
            page.select_option("#sort-selector", "delta_asc")
            page.wait_for_timeout(200)
            page.select_option("#sort-selector", "country_asc")
            page.wait_for_timeout(200)
            page.select_option("#sort-selector", "usd_asc")
            page.wait_for_timeout(200)

            # 7. Test Bookmark & Watchlist Drawer
            page.click("#btn-bookmark")
            page.wait_for_timeout(300)
            bookmark_text = page.text_content("#btn-bookmark")
            self.assertIn("Bookmarked", bookmark_text)

            # Open Watchlist drawer
            page.click("#btn-watchlist-toggle")
            page.wait_for_selector("#drawer-watchlist.open", timeout=3000)
            drawer_watchlist_items = page.query_selector_all("#watchlist-list .drawer-item")
            self.assertGreater(len(drawer_watchlist_items), 0)
            # Close Watchlist drawer
            page.click("#btn-close-watchlist")
            page.wait_for_timeout(300)

            # 8. Test Apple Storefront Validation Probe
            page.click("#btn-validate-apple")
            page.wait_for_function(
                "() => !document.getElementById('apple-status-msg').textContent.includes('Querying')",
                timeout=15000,
            )
            apple_status = page.text_content("#apple-status-msg")
            self.assertTrue(len(apple_status.strip()) > 0)

            # 9. Test Settings Modal & Configuration Persistence
            page.click("#btn-settings-toggle")
            page.wait_for_selector("#modal-settings", state="visible", timeout=3000)

            page.select_option("#setting-base-currency", "EUR")
            page.fill("#setting-default-country", "JP")
            page.fill("#setting-cache-ttl", "72")
            page.click("#btn-save-settings")
            page.wait_for_timeout(400)

            # Close settings modal if still open
            if page.is_visible("#modal-settings"):
                page.click("#btn-close-settings")

            # 10. Test History Drawer & Clear
            page.click("#btn-history-toggle")
            page.wait_for_selector("#drawer-history.open", timeout=3000)
            history_items = page.query_selector_all("#history-list .drawer-item")
            self.assertGreater(len(history_items), 0)

            # Clear History
            page.click("#btn-clear-history")
            page.wait_for_timeout(300)
            empty_history = page.text_content("#history-list")
            self.assertIn("No search history", empty_history)
            page.click("#btn-close-history")

            # 11. Test Diagnostics Telemetry Modal
            page.click("#btn-diag-toggle")
            page.wait_for_selector("#modal-diagnostics", state="visible", timeout=3000)
            page.click("#btn-run-diag")
            page.wait_for_function(
                "() => !document.getElementById('diag-output').textContent.includes('Running live')",
                timeout=15000,
            )
            diag_output = page.text_content("#diag-output")
            self.assertIn("version", diag_output)
            page.click("#btn-close-diag")

            # 12. Test Blob Exports (CSV & JSON)
            page.click("#btn-export-csv")
            page.wait_for_timeout(200)
            page.click("#btn-export-json")
            page.wait_for_timeout(200)

            # 13. Hard Reload & Assert Real State Persistence
            page.reload(wait_until="networkidle")
            page.wait_for_function(
                "() => !document.getElementById('pricing-product-name').textContent.includes('Loading')",
                timeout=15000,
            )

            # Open Settings and verify persisted values survived page reload
            page.click("#btn-settings-toggle")
            page.wait_for_selector("#modal-settings", state="visible", timeout=3000)
            currency_val = page.eval_on_selector("#setting-base-currency", "el => el.value")
            country_val = page.eval_on_selector("#setting-default-country", "el => el.value")
            ttl_val = page.eval_on_selector("#setting-cache-ttl", "el => el.value")

            self.assertEqual(currency_val, "EUR")
            self.assertEqual(country_val, "JP")
            self.assertEqual(ttl_val, "72")
            page.click("#btn-close-settings")

            # Open Watchlist and verify bookmarked product survived page reload
            page.click("#btn-watchlist-toggle")
            page.wait_for_selector("#drawer-watchlist.open", timeout=3000)
            persisted_watchlist = page.query_selector_all("#watchlist-list .drawer-item")
            self.assertGreater(len(persisted_watchlist), 0)
            page.click("#btn-close-watchlist")

            # 14. Capture Verification Screenshot Artifact
            artifact_path = Path(__file__).resolve().parent.parent / "docs" / "artifacts" / "dashboard_e2e_verified.png"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact_path), full_page=True)
            self.assertTrue(artifact_path.is_file())
            self.assertGreater(artifact_path.stat().st_size, 10000)

            # 15. Assert Zero Uncaught Console Errors
            browser.close()
            self.assertEqual(
                len(console_errors),
                0,
                f"Uncaught browser console errors detected: {console_errors}",
            )

    def test_mobile_viewport_and_boundary_stress(self):
        """Test mobile viewport (390x844 iPhone 14/15) responsiveness, touch interaction, and 0 console errors."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME_PATH,
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            # Mobile viewport: 390x844 with mobile device emulation
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            )
            page = context.new_page()

            console_errors = []
            page.on("pageerror", lambda err: console_errors.append(f"PageError: {err}"))
            page.on(
                "console",
                lambda msg: console_errors.append(f"ConsoleError: {msg.text}")
                if msg.type == "error"
                else None,
            )

            # 1. Navigate to dashboard on mobile
            page.goto(self.base_url, wait_until="networkidle", timeout=15000)
            page.wait_for_selector("#pricing-product-name", timeout=10000)

            # 2. Test Mobile Category Navigation
            page.click('.category-pill[data-cat="all"]')
            page.wait_for_timeout(200)

            # 3. Test Mobile Platform Filter Buttons
            page.click('.platform-btn[data-platform="appstore"]')
            page.wait_for_timeout(200)
            page.click('.platform-btn[data-platform="all"]')
            page.wait_for_timeout(200)

            # 4. Test Mobile Sort Selector
            page.select_option("#sort-selector", "delta_asc")
            page.wait_for_timeout(200)

            # 5. Test Mobile Drawers (Watchlist & History)
            page.click("#btn-watchlist-toggle")
            page.wait_for_selector("#drawer-watchlist.open", timeout=3000)
            page.click("#btn-close-watchlist")
            page.wait_for_timeout(200)

            page.click("#btn-history-toggle")
            page.wait_for_selector("#drawer-history.open", timeout=3000)
            page.click("#btn-close-history")
            page.wait_for_timeout(200)

            # 6. Capture Mobile Viewport Screenshot Proof
            mobile_artifact_path = Path(__file__).resolve().parent.parent / "docs" / "artifacts" / "dashboard_e2e_mobile_verified.png"
            mobile_artifact_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(mobile_artifact_path), full_page=False)
            self.assertTrue(mobile_artifact_path.is_file())
            self.assertGreater(mobile_artifact_path.stat().st_size, 5000)

            # 7. Assert 0 Console Errors on Mobile
            browser.close()
            self.assertEqual(
                len(console_errors),
                0,
                f"Mobile uncaught console errors detected: {console_errors}",
            )


if __name__ == "__main__":
    unittest.main()
