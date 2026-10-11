"""Command Line Interface for OpenTheRank CLI (`openrank` / `otr`)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openrank import __version__
from openrank.cache import CacheManager
from openrank.catalog import CatalogError, CatalogManager
from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
)
from openrank.models import (
    AppleValidationResult,
    ProductPricingResult,
    ProductSummary,
)
from openrank.pricing import PricingEngine
from openrank.storefront_apple import (
    KNOWN_APP_IDS,
    AppleStorefrontValidator,
)
from openrank.ui import (
    colorize_delta,
    render_apple_validation,
    render_pricing_table,
    render_search_results,
    should_suppress_color,
)

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130


def get_cache_manager() -> CacheManager:
    """Instantiate CacheManager honoring OPENRANK_CACHE_DIR and OPENRANK_CACHE_TTL env vars."""
    cache_dir = os.environ.get("OPENRANK_CACHE_DIR")
    ttl_str = os.environ.get("OPENRANK_CACHE_TTL")
    ttl = int(ttl_str) if ttl_str and ttl_str.isdigit() else 86400
    return CacheManager(cache_dir=cache_dir, default_ttl=ttl)


def get_catalog_manager(refresh: bool = False) -> CatalogManager:
    """Instantiate CatalogManager with project configuration."""
    cache = get_cache_manager()
    return CatalogManager(cache_manager=cache)


def cmd_search(args: argparse.Namespace) -> int:
    """Handle `openrank search <query>`."""
    query = args.query.strip()
    extra_tokens = getattr(args, "extra_query", None)
    if extra_tokens:
        query = f"{query} {' '.join(extra_tokens)}".strip()
    as_json = getattr(args, "json", False)
    no_color = getattr(args, "no_color", False)
    limit = getattr(args, "limit", 10)
    category = getattr(args, "category", None)
    refresh = getattr(args, "refresh", False)

    catalog_mgr = get_catalog_manager()
    try:
        results = catalog_mgr.search(
            query=query,
            category=category,
            limit=limit,
            refresh=refresh,
        )
    except CatalogError as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "CATALOG_ERROR"}))
        else:
            sys.stderr.write(f"Catalog Error: {e}\n")
        return EXIT_ERROR
    except (NetworkError, OSError) as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "NETWORK_ERROR"}))
        else:
            sys.stderr.write(f"Network Error: {e}\n")
        return EXIT_ERROR

    output = render_search_results(results, as_json=as_json, no_color=no_color)
    print(output)
    return EXIT_SUCCESS


def cmd_pricing(args: argparse.Namespace) -> int:
    """Handle `openrank pricing <product>`."""
    product_query = args.product.strip()
    extra_tokens = getattr(args, "extra_product", None)
    if extra_tokens:
        product_query = f"{product_query} {' '.join(extra_tokens)}".strip()
    as_json = getattr(args, "json", False)
    no_color = getattr(args, "no_color", False)
    platform = getattr(args, "platform", "all")
    country = getattr(args, "country", None)
    source = getattr(args, "source", "otr")
    validate_apple = getattr(args, "validate_apple", False)
    refresh = getattr(args, "refresh", False)

    if country:
        country = country.strip().upper()

    catalog_mgr = get_catalog_manager()

    # 1. Resolve Product from Catalog
    try:
        product = catalog_mgr.resolve_product(product_query, refresh=refresh)
    except CatalogError as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "CATALOG_ERROR"}))
        else:
            sys.stderr.write(f"Catalog Error: {e}\n")
        return EXIT_ERROR
    except (NetworkError, OSError) as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "NETWORK_ERROR"}))
        else:
            sys.stderr.write(f"Network Error: {e}\n")
        return EXIT_ERROR

    # If product not found in catalog, check fallback for direct Apple source
    if product is None:
        norm_slug = product_query.lower()
        if source == "apple" and norm_slug in KNOWN_APP_IDS:
            product = ProductSummary(
                name=product_query,
                slug=norm_slug,
                path=f"/pricing/{norm_slug}/",
                category="Apps",
                url=f"https://opentherank.com/pricing/{norm_slug}/",
            )
        else:
            suggestions = catalog_mgr.search(product_query, limit=3)
            if as_json:
                print(
                    json.dumps(
                        {
                            "error": True,
                            "message": f"Product '{product_query}' not found in catalog",
                            "code": "PRODUCT_NOT_FOUND",
                            "suggestions": [
                                {
                                    "name": s[0].name,
                                    "slug": s[0].slug,
                                    "category": s[0].category,
                                }
                                for s in suggestions
                            ],
                        },
                        indent=2,
                    )
                )
            else:
                sys.stderr.write(f"Error: Product '{product_query}' not found in catalog.\n")
                if suggestions:
                    suggest_names = ", ".join(f"'{s[0].name}'" for s in suggestions)
                    sys.stderr.write(f"Did you mean: {suggest_names}?\n")
            return EXIT_ERROR

    # 2. Branch: First-Party Apple Storefront Query (--source apple)
    if source == "apple":
        apple_country = (country or "US").lower()
        validator = AppleStorefrontValidator()
        val_result = validator.validate_product(product.slug, apple_country)
        output = render_apple_validation(val_result, as_json=as_json, no_color=no_color)
        if val_result.is_spoofed or not val_result.is_valid or val_result.error:
            if as_json:
                print(output)
            else:
                sys.stderr.write(output + "\n")
            return EXIT_ERROR
        print(output)
        return EXIT_SUCCESS

    # 3. Branch: OpenTheRank Multi-Region Pricing Query
    pricing_engine = PricingEngine()
    try:
        pricing_result = pricing_engine.fetch_pricing(
            product=product,
            platform=platform,
            country=country,
        )
    except HttpFastFailError as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "FAST_FAIL_ERROR", "status_code": e.status_code}))
        else:
            sys.stderr.write(f"HTTP Error {e.status_code}: {e.message}\n")
        return EXIT_ERROR
    except (NetworkError, OSError) as e:
        if as_json:
            print(json.dumps({"error": True, "message": str(e), "code": "NETWORK_ERROR"}))
        else:
            sys.stderr.write(f"Network Error: {e}\n")
        return EXIT_ERROR

    # 4. Optional Cross-Validation with Apple App Store
    if validate_apple:
        target_country = country or "US"
        validator = AppleStorefrontValidator()
        validator.cross_validate_pricing(pricing_result, target_country)

    output = render_pricing_table(pricing_result, as_json=as_json, no_color=no_color)
    print(output)
    return EXIT_SUCCESS


def cmd_serve(args: argparse.Namespace) -> int:
    """Handle `openrank serve`."""
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8765)
    no_open = getattr(args, "no_open", False)

    from openrank.server import OpenRankApiHandler, ThreadingHTTPServer

    try:
        server = ThreadingHTTPServer((host, port), OpenRankApiHandler)
    except OSError as e:
        sys.stderr.write(f"Error starting server on {host}:{port}: {e}\n")
        return EXIT_ERROR

    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"OpenTheRank Web Dashboard running at: {url}")
    print("Serving interactive pricing dashboard & REST API...")
    print("Press Ctrl+C to stop.")

    if not no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()
    return EXIT_SUCCESS


def build_parser() -> argparse.ArgumentParser:
    """Build the root ArgumentParser with subcommands and flags."""
    parser = argparse.ArgumentParser(
        prog="openrank",
        description="OpenTheRank CLI — Global pricing, subscription rankings, and currency arbitrage spreads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program version and exit.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="subcommands",
        description="Available commands",
        help="Command to execute (run '<command> --help' for details)",
    )

    # Subcommand: search (alias: s)
    search_p = subparsers.add_parser(
        "search",
        aliases=["s"],
        help="Search and resolve products by name, slug, or category",
        description="Search OpenTheRank product catalog with multi-tier fuzzy matching.",
    )
    search_p.add_argument(
        "query",
        type=str,
        help="Product name, slug, or keywords (e.g. 'netflix', 'chatgpt')",
    )
    search_p.add_argument(
        "extra_query",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    search_p.add_argument(
        "-c",
        "--category",
        help="Filter products by category (e.g. 'AI Subscriptions', 'Streaming')",
    )
    search_p.add_argument(
        "-n",
        "--limit",
        type=int,
        default=10,
        help="Maximum results to return (default: 10)",
    )
    search_p.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass cache and force fresh catalog retrieval from remote",
    )
    search_p.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON to stdout",
    )
    search_p.add_argument(
        "--no-color",
        action="store_true",
        help="Suppress ANSI styling in terminal output",
    )
    search_p.set_defaults(command="search", handler=cmd_search)

    # Subcommand: pricing (alias: p)
    pricing_p = subparsers.add_parser(
        "pricing",
        aliases=["p"],
        help="Query regional pricing, discounts, and arbitrage spreads for a product",
        description="Fetch and render real-time multi-region pricing rankings for a product.",
    )
    pricing_p.add_argument(
        "product",
        type=str,
        help="Product name or slug (e.g. 'chatgpt', 'netflix', 'gemini')",
    )
    pricing_p.add_argument(
        "extra_product",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    pricing_p.add_argument(
        "--platform",
        choices=["all", "appstore", "play", "web", "direct"],
        default="all",
        type=str.lower,
        help="Filter pricing by storefront platform (default: all)",
    )
    pricing_p.add_argument(
        "--country",
        help="Filter results by 2-letter ISO country code (e.g. 'PH', 'TR', 'US')",
    )
    pricing_p.add_argument(
        "--source",
        choices=["all", "otr", "opentherank", "apple"],
        default="otr",
        type=str.lower,
        help="Data source: 'otr' (OpenTheRank tables) or 'apple' (Apple App Store)",
    )
    pricing_p.add_argument(
        "--validate-apple",
        action="store_true",
        help="Cross-validate OpenTheRank pricing against official Apple Storefront IAPs",
    )
    pricing_p.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass cache and force fresh data retrieval",
    )
    pricing_p.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON to stdout",
    )
    pricing_p.add_argument(
        "--no-color",
        action="store_true",
        help="Suppress ANSI styling in terminal output",
    )
    pricing_p.set_defaults(command="pricing", handler=cmd_pricing)

    # Subcommand: serve (aliases: srv, web)
    serve_p = subparsers.add_parser(
        "serve",
        aliases=["srv", "web"],
        help="Launch the interactive Web Dashboard & REST API server",
        description="Start the local zero-dependency HTTP server hosting the OpenTheRank Web Dashboard.",
    )
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address to bind the HTTP server to (default: 127.0.0.1)",
    )
    serve_p.add_argument(
        "-p",
        "--port",
        type=int,
        default=8765,
        help="Port number to listen on (default: 8765, pass 0 for dynamic port)",
    )
    serve_p.add_argument(
        "--no-open",
        action="store_true",
        help="Do not automatically launch web browser after starting server",
    )
    serve_p.set_defaults(command="serve", handler=cmd_serve)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main CLI entrypoint.

    Returns exit code (0: Success, 1: Error, 2: Usage, 130: Interrupt).
    """
    parser = build_parser()

    if argv is None:
        if len(sys.argv) <= 1:
            parser.print_help()
            return EXIT_SUCCESS
        raw_args = sys.argv[1:]
    else:
        if len(argv) == 0:
            parser.print_help()
            return EXIT_SUCCESS
        raw_args = list(argv)

    try:
        args = parser.parse_args(raw_args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_SUCCESS

    try:
        return args.handler(args)
    except KeyboardInterrupt:
        if not getattr(args, "json", False):
            sys.stderr.write("\nOperation aborted by user.\n")
        return EXIT_INTERRUPT
    except Exception as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": True, "message": str(e), "code": "UNEXPECTED_ERROR"}))
        else:
            sys.stderr.write(f"Unexpected Error: {e}\n")
        return EXIT_ERROR


__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPT",
    "EXIT_SUCCESS",
    "EXIT_USAGE",
    "build_parser",
    "cmd_pricing",
    "cmd_search",
    "cmd_serve",
    "get_cache_manager",
    "get_catalog_manager",
    "main",
]
