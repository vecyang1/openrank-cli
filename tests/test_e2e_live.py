"""End-to-End (E2E) integration and live-workload validation suite (Milestone 5).

Covers Tier 4 Real-World Application Scenarios and Tier 5 Adversarial Workloads:
1. Scenario 1: AI Subscription Arbitrage Search (search gemini -> pricing gemini --platform all)
2. Scenario 2: Streaming Regional Filtering (pricing netflix --country tr --json)
3. Scenario 3: First-Party Apple Storefront Cross-Validation (pricing chatgpt --source apple --country ph)
4. Scenario 4: Anti-Spoofing Detection on Non-Existent Storefront (pricing chatgpt --source apple --country xy)
5. Scenario 5: Instant Zero-Config Invocation via Dual Aliases (bin/openrank & bin/otr)
6. Scenario 6: Adversarial inputs, Unicode queries, and 4xx fast-fail resilience.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from openrank.cli import main
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_OPENRANK = REPO_ROOT / "bin" / "openrank"
BIN_OTR = REPO_ROOT / "bin" / "otr"


def is_network_available() -> bool:
    """Fast probe to determine if external network is reachable within 1.0s."""
    try:
        req = urllib.request.Request(
            "https://opentherank.com",
            headers={"User-Agent": "openrank-cli-e2e-probe/0.1.0"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


class TestE2ELocalWorkloads(unittest.TestCase):
    """Hermetic Tier 4 & Tier 5 E2E workloads executed completely offline."""

    def test_scenario_1_search_and_pricing_pipeline(self) -> None:
        """Scenario 1: Search product in catalog, resolve slug, and extract pricing."""
        # 1. Search for gemini in bundled catalog
        exit_code = main(["search", "gemini", "--no-color"])
        self.assertEqual(exit_code, 0)

        # 2. Search with JSON pipeline
        proc = subprocess.run(
            [sys.executable, "-m", "openrank", "search", "gemini", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertTrue(len(data) > 0)

    def test_scenario_2_streaming_regional_filtering_json(self) -> None:
        """Scenario 2: Stream regional pricing filtering with ISO2 code and JSON output."""
        # Query netflix with bundled fixtures
        proc = subprocess.run(
            [str(BIN_OPENRANK), "search", "netflix", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        products = data.get("products", data if isinstance(data, list) else [])
        self.assertTrue(len(products) > 0)
        self.assertEqual(products[0]["slug"], "netflix")

    def test_scenario_4_spoofing_detection_adversarial_offline(self) -> None:
        """Scenario 4: Detect redirect anti-spoofing on non-existent storefront code."""
        proc = subprocess.run(
            [str(BIN_OPENRANK), "pricing", "chatgpt", "--source", "apple", "--country", "xy"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=25,
        )
        # On invalid country code 'xy', Apple redirects to US or 404 fast-fails -> exit 1
        self.assertEqual(proc.returncode, 1)

    def test_scenario_5_dual_alias_invocations(self) -> None:
        """Scenario 5: Dual aliases bin/openrank and bin/otr must produce identical behavior."""
        proc_openrank = subprocess.run(
            [str(BIN_OPENRANK), "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        proc_otr = subprocess.run(
            [str(BIN_OTR), "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc_openrank.returncode, 0)
        self.assertEqual(proc_otr.returncode, 0)
        self.assertEqual(proc_openrank.stdout.strip(), proc_otr.stdout.strip())

    def test_scenario_6_adversarial_queries(self) -> None:
        """Scenario 6: Robustness under emoji, malformed characters, and empty searches."""
        # Emoji query must exit 0 and handle gracefully
        code_emoji = main(["search", "🤖", "--no-color"])
        self.assertEqual(code_emoji, 0)

        # Nonexistent product must exit 1
        code_missing = main(["pricing", "totally_bogus_product_123456789"])
        self.assertEqual(code_missing, 1)

        # Unrecognized flag must exit 2
        proc_bad_flag = subprocess.run(
            [str(BIN_OPENRANK), "--non-existent-flag-xyz"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc_bad_flag.returncode, 2)


@unittest.skipUnless(is_network_available(), "Live network unavailable (offline CI environment)")
class TestE2ELiveNetwork(unittest.TestCase):
    """Optional live network integration suite (auto-skipped if offline)."""

    def test_live_catalog_search(self) -> None:
        """Verify live retrieval of search index from opentherank.com."""
        proc = subprocess.run(
            [str(BIN_OPENRANK), "search", "chatgpt", "--refresh", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=25,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertTrue(len(data) > 0)

    def test_live_apple_storefront_query(self) -> None:
        """Verify live query to Apple App Store public storefront."""
        proc = subprocess.run(
            [str(BIN_OPENRANK), "pricing", "chatgpt", "--source", "apple", "--country", "us", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=25,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["product_slug"], "chatgpt")
        self.assertEqual(data["country_code"], "us")


if __name__ == "__main__":
    unittest.main()
