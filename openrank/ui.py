"""Terminal UI rendering, table formatting, ANSI highlights, and JSON serialization."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Helper to remove ANSI escape sequences from rendered output."""
    return ANSI_ESCAPE_RE.sub("", text)


def should_suppress_color(no_color: bool = False) -> bool:
    """Determine whether ANSI color escapes should be suppressed."""
    if no_color:
        return True
    if os.environ.get("NO_COLOR", "").strip():
        return True
    if os.environ.get("TERM") == "dumb":
        return True
    return False


def colorize_delta(delta: Optional[float], no_color: bool = False) -> str:
    """Render a price delta percentage with ANSI color codes.

    - Negative delta (discount/savings): Green
    - Positive delta (markup/premium): Red
    - Zero delta (parity): Neutral/Dim
    """
    if delta is None:
        return "N/A"
    delta_str = f"{delta:+.1f}%"
    if no_color:
        return delta_str
    if delta < 0:
        return f"\x1b[32m{delta_str}\x1b[0m"
    elif delta > 0:
        return f"\x1b[31m{delta_str}\x1b[0m"
    else:
        return f"\x1b[90m{delta_str}\x1b[0m"


def render_search_results(
    results: Sequence[Tuple[ProductSummary, int]],
    as_json: bool = False,
    no_color: bool = False,
) -> str:
    """Format catalog search results into an ASCII table or JSON."""
    if as_json:
        data = {
            "count": len(results),
            "products": [
                {
                    "name": p.name,
                    "slug": p.slug,
                    "path": p.path,
                    "category": p.category,
                    "url": p.url,
                    "score": score,
                }
                for p, score in results
            ],
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    if not results:
        return "No matching products found."

    term_width = shutil.get_terminal_size((80, 24)).columns
    lines: List[str] = []
    header = f"{'NAME':<25} {'CATEGORY':<20} {'SLUG':<20} {'SCORE':<6}"
    lines.append(header[:term_width] if term_width < len(header) else header)
    sep = "-" * min(term_width, 75)
    lines.append(sep)
    for p, score in results:
        row = f"{p.name:<25} {p.category:<20} {p.slug:<20} {score:<6}"
        lines.append(row[:term_width] if term_width < len(row) else row)
    return "\n".join(lines)


def render_pricing_table(
    result: ProductPricingResult,
    as_json: bool = False,
    no_color: bool = False,
) -> str:
    """Format multi-region pricing results into an ASCII table or JSON."""
    if as_json:
        return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)

    if not result.records:
        return f"No pricing records found for {result.product.name}."

    effective_no_color = should_suppress_color(no_color)
    term_width = shutil.get_terminal_size((80, 24)).columns
    lines: List[str] = [f"=== Regional Pricing for {result.product.name} ==="]
    if result.baseline_us_price is not None:
        lines.append(f"US Baseline: ${result.baseline_us_price:.2f}")
    if result.first_party_validated:
        lines.append("[✓ Apple Storefront Verified]")
    for note in result.validation_notes:
        lines.append(f"  Note: {note}")
    lines.append("")

    header = f"{'CC':<4} {'REGION':<18} {'PLATFORM':<10} {'PRICE (LOCAL)':<18} {'USD':<10} {'VS US %':<10} {'RISK':<8}"
    lines.append(header[:term_width] if term_width < len(header) else header)
    sep = "-" * min(term_width, 82)
    lines.append(sep)

    for r in result.records:
        delta_str = colorize_delta(r.vs_us_delta, no_color=effective_no_color)
        usd_str = f"${r.primary_tier.usd_price:.2f}"
        row = (
            f"{r.country_code:<4} {r.region_name:<18} {r.platform:<10} "
            f"{r.primary_tier.local_price_str:<18} {usd_str:<10} "
            f"{delta_str:<10} {r.risk_level:<8}"
        )
        if term_width < 60:
            plain_row = strip_ansi(row)
            lines.append(plain_row[:term_width])
        else:
            lines.append(row)

    return "\n".join(lines)


def render_apple_validation(
    result: AppleValidationResult,
    as_json: bool = False,
    no_color: bool = False,
) -> str:
    """Format Apple App Store storefront validation results into text or JSON."""
    if as_json:
        return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)

    lines: List[str] = [
        f"=== Apple Storefront Validation: {result.product_slug} ({result.country_code.upper()}) ==="
    ]
    if result.is_spoofed:
        lines.append(f"[!] ANTI-SPOOFING ALERT: Redirected to {result.final_url or 'US store'}")
    if result.error:
        lines.append(f"Error: {result.error}")
    lines.append(f"App ID: {result.app_id} | Status: {result.status_code}")

    if result.items:
        lines.append("")
        lines.append(f"{'TIER / IAP NAME':<35} {'LOCAL PRICE':<18} {'NUMERIC':<10} {'CURRENCY':<8}")
        lines.append("-" * 75)
        for item in result.items:
            num = f"{item.numeric_price:.2f}" if item.numeric_price is not None else "N/A"
            sym = item.currency_symbol or "N/A"
            lines.append(f"{item.name:<35} {item.local_price_str:<18} {num:<10} {sym:<8}")

    return "\n".join(lines)


__all__ = [
    "ANSI_ESCAPE_RE",
    "colorize_delta",
    "render_apple_validation",
    "render_pricing_table",
    "render_search_results",
    "should_suppress_color",
    "strip_ansi",
]
