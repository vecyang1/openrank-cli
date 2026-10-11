"""Comprehensive test suite blueprint for openrank.cli (Argparse, Execution, Entrypoints).

Tests cover:
- F14: CLI Subcommands & Flags: `search`, `pricing`, aliases `s` and `p`, `--platform`, `--country`, `--source`, `--json`, `--refresh`, `--no-color`.
- F14/F15: Standard Exit Codes: 0 (success), 1 (operational/not found/network error), 2 (syntax/argparse error), 130 (SIGINT/KeyboardInterrupt).
- F15: Dual Entrypoint Packaging: `python3 -m openrank`, `bin/openrank` (chmod +x), `bin/otr` (chmod +x), and execution from external cwd.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openrank.client import NetworkError
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestCliParser(unittest.TestCase):
    """Test CLI argument parsing and subcommand definition (F14)."""

    def setUp(self) -> None:
        try:
            import openrank.cli as cli
            self.cli = cli
        except ImportError:
            self.skipTest("openrank.cli module not yet implemented")

    def test_parser_version_flag(self) -> None:
        """--version or -v prints version string and exits with code 0."""
        parser = self.cli.build_parser() if hasattr(self.cli, "build_parser") else None
        if parser is None:
            self.skipTest("build_parser not found on cli module")

        with self.assertRaises(SystemExit) as ctx, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_parser_subcommands_and_aliases(self) -> None:
        """Parser must recognize search, pricing, and their aliases s, p."""
        parser = self.cli.build_parser()

        # search and s alias
        args1 = parser.parse_args(["search", "chatgpt"])
        self.assertEqual(args1.command, "search")
        self.assertEqual(args1.query, "chatgpt")

        args2 = parser.parse_args(["s", "gemini"])
        self.assertEqual(args2.command, "search")
        self.assertEqual(args2.query, "gemini")

        # pricing and p alias
        args3 = parser.parse_args(["pricing", "netflix"])
        self.assertEqual(args3.command, "pricing")
        self.assertEqual(args3.product, "netflix")

        args4 = parser.parse_args(["p", "claude"])
        self.assertEqual(args4.command, "pricing")
        self.assertEqual(args4.product, "claude")

    def test_parser_search_options(self) -> None:
        """Search command options (--limit, --category, --json, --refresh, --no-color)."""
        parser = self.cli.build_parser()
        args = parser.parse_args([
            "search", "youtube",
            "--limit", "5",
            "--category", "Streaming",
            "--json",
            "--refresh",
            "--no-color",
        ])
        self.assertEqual(args.query, "youtube")
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.category, "Streaming")
        self.assertTrue(args.json)
        self.assertTrue(args.refresh)
        self.assertTrue(args.no_color)

    def test_parser_pricing_options(self) -> None:
        """Pricing command options (--platform, --country, --source, --json, --refresh)."""
        parser = self.cli.build_parser()
        args = parser.parse_args([
            "pricing", "chatgpt",
            "--platform", "appstore",
            "--country", "ph",
            "--source", "apple",
            "--json",
            "--refresh",
        ])
        self.assertEqual(args.product, "chatgpt")
        self.assertEqual(args.platform, "appstore")
        self.assertEqual(args.country, "ph")
        self.assertEqual(args.source, "apple")
        self.assertTrue(args.json)
        self.assertTrue(args.refresh)

    def test_parser_serve_options(self) -> None:
        """Serve command options (--host, -p/--port, --no-open) and aliases (srv, web)."""
        parser = self.cli.build_parser()

        args1 = parser.parse_args(["serve"])
        self.assertEqual(args1.command, "serve")
        self.assertEqual(args1.host, "127.0.0.1")
        self.assertEqual(args1.port, 8765)
        self.assertFalse(args1.no_open)

        args2 = parser.parse_args(["srv", "--host", "0.0.0.0", "-p", "9000", "--no-open"])
        self.assertEqual(args2.command, "serve")
        self.assertEqual(args2.host, "0.0.0.0")
        self.assertEqual(args2.port, 9000)
        self.assertTrue(args2.no_open)

        args3 = parser.parse_args(["web", "-p", "0"])
        self.assertEqual(args3.command, "serve")
        self.assertEqual(args3.port, 0)

    def test_parser_invalid_subcommand_exits_2(self) -> None:
        """Unrecognized subcommand must exit with code 2 (argparse standard)."""
        parser = self.cli.build_parser()
        with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO):
            parser.parse_args(["unknown_command_xyz"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parser_missing_required_positional_exits_2(self) -> None:
        """Missing required argument for search or pricing must exit with code 2."""
        parser = self.cli.build_parser()
        with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO):
            parser.parse_args(["pricing"])
        self.assertEqual(ctx.exception.code, 2)


class TestCliExecution(unittest.TestCase):
    """Test CLI main() execution, subcommand logic, and exit codes (F14)."""

    def setUp(self) -> None:
        try:
            import openrank.cli as cli
            self.cli = cli
        except ImportError:
            self.skipTest("openrank.cli module not yet implemented")

    def test_main_search_success_exits_0(self) -> None:
        """Executing search with matches must print results and return exit code 0."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        with patch("openrank.catalog.CatalogManager.search", return_value=[(mock_prod, 100)]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = self.cli.main(["search", "chatgpt", "--no-color"])
            self.assertEqual(code, 0)
            self.assertIn("ChatGPT", mock_out.getvalue())

    def test_main_search_json_success_exits_0(self) -> None:
        """Executing search with --json must print valid JSON and return exit code 0."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        with patch("openrank.catalog.CatalogManager.search", return_value=[(mock_prod, 100)]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = self.cli.main(["search", "chatgpt", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(mock_out.getvalue())
            self.assertTrue(len(data) > 0)

    def test_main_search_empty_results_exits_0(self) -> None:
        """Search query with zero results should exit cleanly with code 0."""
        with patch("openrank.catalog.CatalogManager.search", return_value=[]), \
             patch("sys.stdout", new_callable=io.StringIO):
            code = self.cli.main(["search", "nonexistentquery12345"])
            self.assertEqual(code, 0)

    def test_main_pricing_success_exits_0(self) -> None:
        """Executing pricing for resolved product prints pricing table and returns 0."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        mock_record = RegionPricingRecord(
            country_code="US", region_name="United States", platform="appstore",
            primary_tier=PriceTier(name="Plus", local_price_str="$20.00", usd_price=20.0, delta_percent=0.0),
            vs_us_delta=0.0,
        )
        mock_res = ProductPricingResult(product=mock_prod, records=[mock_record], baseline_us_price=20.0)

        with patch("openrank.catalog.CatalogManager.resolve_product", return_value=mock_prod), \
             patch("openrank.pricing.PricingEngine.fetch_pricing", return_value=mock_res), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = self.cli.main(["pricing", "chatgpt", "--no-color"])
            self.assertEqual(code, 0)
            self.assertIn("ChatGPT", mock_out.getvalue())
            self.assertIn("United States", mock_out.getvalue())

    def test_main_pricing_product_not_found_exits_1(self) -> None:
        """Pricing command on an unresolvable product must write error to stderr and exit 1."""
        with patch("openrank.catalog.CatalogManager.resolve_product", return_value=None), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            code = self.cli.main(["pricing", "completely_fake_product_xyz"])
            self.assertEqual(code, 1)
            self.assertTrue("not found" in mock_err.getvalue().lower())

    def test_main_pricing_network_error_exits_1(self) -> None:
        """Network error during pricing fetch must catch cleanly, print error, and exit 1."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        with patch("openrank.catalog.CatalogManager.resolve_product", return_value=mock_prod), \
             patch("openrank.pricing.PricingEngine.fetch_pricing", side_effect=NetworkError("Connection refused")), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            code = self.cli.main(["pricing", "chatgpt"])
            self.assertEqual(code, 1)
            self.assertIn("Connection refused", mock_err.getvalue())

    def test_main_pricing_apple_validation_valid_exits_0(self) -> None:
        """Valid Apple validation with --source apple returns 0."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        mock_val = AppleValidationResult(
            product_slug="chatgpt", country_code="ph", is_valid=True, is_spoofed=False,
            items=[AppleIAPItem(name="Plus", local_price_str="₱999", numeric_price=999.0)],
        )
        with patch("openrank.catalog.CatalogManager.resolve_product", return_value=mock_prod), \
             patch("openrank.storefront_apple.AppleStorefrontValidator.validate_product", return_value=mock_val), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = self.cli.main(["pricing", "chatgpt", "--source", "apple", "--country", "ph"])
            self.assertEqual(code, 0)
            self.assertIn("Plus", mock_out.getvalue())

    def test_main_pricing_apple_validation_spoofed_exits_1(self) -> None:
        """Spoofed Apple validation (redirected to US) must alert and return exit code 1."""
        mock_prod = ProductSummary(name="ChatGPT", slug="chatgpt", path="/ai/chatgpt/", category="AI", url="")
        mock_val = AppleValidationResult(
            product_slug="chatgpt", country_code="xy", is_valid=False, is_spoofed=True,
            final_url="https://apps.apple.com/us/app/chatgpt/id6448311069",
            error="Redirected to US store",
        )
        with patch("openrank.catalog.CatalogManager.resolve_product", return_value=mock_prod), \
             patch("openrank.storefront_apple.AppleStorefrontValidator.validate_product", return_value=mock_val), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            code = self.cli.main(["pricing", "chatgpt", "--source", "apple", "--country", "xy"])
            self.assertEqual(code, 1)

    def test_main_keyboard_interrupt_exits_130(self) -> None:
        """KeyboardInterrupt / SIGINT must exit with code 130 and clean abort message."""
        with patch("openrank.catalog.CatalogManager.resolve_product", side_effect=KeyboardInterrupt), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            code = self.cli.main(["pricing", "chatgpt"])
            self.assertEqual(code, 130)
            self.assertTrue("aborted" in mock_err.getvalue().lower() or "interrupted" in mock_err.getvalue().lower())


class TestCliEntrypointsSubprocess(unittest.TestCase):
    """Test standalone binary wrappers, permissions, and subprocess invocation (F15)."""

    def setUp(self) -> None:
        self.bin_openrank = REPO_ROOT / "bin" / "openrank"
        self.bin_otr = REPO_ROOT / "bin" / "otr"
        self.main_py = REPO_ROOT / "openrank" / "__main__.py"

        if not self.bin_openrank.is_file() or not self.bin_otr.is_file() or not self.main_py.is_file():
            self.skipTest("Entrypoints bin/openrank, bin/otr, or __main__.py not yet created")

    def test_executable_permissions_bit(self) -> None:
        """Verify bin/openrank and bin/otr have executable bits set (chmod +x)."""
        mode_openrank = os.stat(self.bin_openrank).st_mode
        mode_otr = os.stat(self.bin_otr).st_mode

        self.assertTrue(bool(mode_openrank & stat.S_IXUSR), "bin/openrank must have user execute bit (chmod +x)")
        self.assertTrue(bool(mode_otr & stat.S_IXUSR), "bin/otr must have user execute bit (chmod +x)")

    def test_subprocess_python_m_openrank_version(self) -> None:
        """Running 'python3 -m openrank --version' must succeed and exit with 0."""
        proc = subprocess.run(
            [sys.executable, "-m", "openrank", "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0.1.0", proc.stdout + proc.stderr)

    def test_subprocess_bin_openrank_version(self) -> None:
        """Running './bin/openrank --version' must succeed and exit with 0."""
        proc = subprocess.run(
            [str(self.bin_openrank), "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0.1.0", proc.stdout + proc.stderr)

    def test_subprocess_bin_otr_version(self) -> None:
        """Running './bin/otr --version' must succeed and exit with 0."""
        proc = subprocess.run(
            [str(self.bin_otr), "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0.1.0", proc.stdout + proc.stderr)

    def test_subprocess_bin_openrank_from_external_cwd(self) -> None:
        """Running bin/openrank from an external cwd (e.g. /tmp) must bootstrap sys.path and succeed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc = subprocess.run(
                [str(self.bin_openrank), "search", "chatgpt", "--json"],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, f"Failed when run from external cwd: {proc.stderr}")
            data = json.loads(proc.stdout)
            self.assertTrue(len(data) > 0)

    def test_subprocess_bin_otr_from_external_cwd(self) -> None:
        """Running bin/otr from an external cwd must bootstrap sys.path and succeed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc = subprocess.run(
                [str(self.bin_otr), "search", "netflix", "--json"],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, f"Failed when run from external cwd: {proc.stderr}")
            data = json.loads(proc.stdout)
            self.assertTrue(len(data) > 0)


if __name__ == "__main__":
    unittest.main()
