"""First-Party Apple Storefront Cross-Validation & Anti-Spoofing Engine."""
from __future__ import annotations

import html
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
)
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    ProductPricingResult,
)

KNOWN_APP_IDS: Dict[str, str] = {
    "chatgpt": "id6448311069",
    "chatgpt-plus": "id6448311069",
    "claude": "id6473753684",
    "claude-pro": "id6473753684",
    "gemini": "id6477507566",
    "google-gemini": "id6477507566",
    "youtube": "id544007664",
    "youtube-premium": "id544007664",
    "netflix": "id363590051",
    "spotify": "id324684580",
    "duolingo": "id570060128",
    "tinder": "id547702041",
    "perplexity": "id1667443344",
}


# Precompiled regular expressions for high-throughput currency and price parsing
_UNICODE_WS_RE = re.compile(r"[\u00a0\u2009\u202f]+")
_DIGIT_RE = re.compile(r"[0-9]")
_FREE_WORD_RE = re.compile(r"\b(?:free|gratis|kostenlos|gratuit)\b", flags=re.IGNORECASE)
_BILLING_PERIOD_RE = re.compile(r"/(?:mo|month|yr|year|week|day)\b", flags=re.IGNORECASE)
_PROMO_RE = re.compile(
    r"\b(?:"
    r"free\s+trial"
    r"|try\s+free(?:\s+for\s+\d+\s+days?)?"
    r"|\d+-day\s+free\s+trial"
    r"|ad-free|ad\s+free"
    r"|toll-free"
    r"|risk-free(?:\s+trial)?"
    r"|freemium"
    r")\b",
    flags=re.IGNORECASE,
)
_THEN_AFTER_RE = re.compile(r"\b(?:then|after(?:\s+trial)?)\b", flags=re.IGNORECASE)
_LEADING_PUNCT_RE1 = re.compile(r"^[+\-\s]+")
_LEADING_PUNCT_RE2 = re.compile(r"^[,\s]+(?![0-9])")
_PRICE_NUM_RE = re.compile(r"[-+]?\s*([0-9]+[0-9.,\s]*|[.,][0-9]+[0-9.,\s]*)")
_ALL_WS_RE = re.compile(r"\s+")


def parse_price_and_currency(price_str: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse local price string into numeric float and currency symbol.

    Examples:
        "$19.99" -> (19.99, "$")
        "$.99" -> (0.99, "$")
        "1\u00a0490,00 €" -> (1490.0, "€")
        "Free trial then $19.99" -> (19.99, "$")
        "Free" -> (0.0, None)
    """
    if not price_str or not isinstance(price_str, str):
        return None, None
    s = price_str.strip()
    if not s:
        return None, None

    # Normalize Unicode whitespace (non-breaking \u00a0, thin \u2009, narrow no-break \u202f) to standard space
    if "\u00a0" in s or "\u2009" in s or "\u202f" in s:
        s = _UNICODE_WS_RE.sub(" ", s)

    # Check for genuine standalone free tier (without price digits)
    if not _DIGIT_RE.search(s):
        if _FREE_WORD_RE.search(s):
            return 0.0, None
        return None, None

    # Remove recurring billing period markers and approximation glyphs
    cleaned = _BILLING_PERIOD_RE.sub("", s)
    if "≈" in cleaned or "~" in cleaned:
        cleaned = cleaned.replace("≈", "").replace("~", "").strip()
    else:
        cleaned = cleaned.strip()

    # Strip promotional prefixes and phrases (e.g. 'free trial', 'ad-free', 'try free for 7 days, then')
    lower_cleaned = cleaned.lower()
    if "free" in lower_cleaned or "trial" in lower_cleaned or "freemium" in lower_cleaned:
        cleaned = _PROMO_RE.sub("", cleaned)
        cleaned = _THEN_AFTER_RE.sub("", cleaned)
        cleaned = cleaned.strip()

    if cleaned.startswith(("+", "-", " ", ",", "\t")):
        cleaned = _LEADING_PUNCT_RE1.sub("", cleaned)
        cleaned = _LEADING_PUNCT_RE2.sub("", cleaned)
        cleaned = cleaned.strip()

    # Search for numeric substring supporting leading integer or leading decimal (.99, ,99, $.99)
    match = _PRICE_NUM_RE.search(cleaned)
    if not match:
        return None, None

    num_raw = match.group(0).strip()
    prefix = cleaned[: match.start()].strip()
    suffix = cleaned[match.end() :].strip()

    symbol = None
    if prefix:
        cand = prefix.split()[-1].strip("()[]{} ,+-")
        if cand:
            symbol = cand
    if not symbol and suffix:
        cand = suffix.split()[0].strip("()[]{} ,+-")
        if cand:
            symbol = cand

    # Normalize separators and strip all whitespace (ASCII and Unicode)
    n = _ALL_WS_RE.sub("", num_raw)
    if "," in n and "." in n:
        if n.rfind(",") < n.rfind("."):
            # 1,490.00 -> comma is thousands separator
            n = n.replace(",", "")
        else:
            # 1.490,00 -> dot is thousands separator, comma is decimal
            n = n.replace(".", "").replace(",", ".")
    elif "," in n:
        parts = n.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            # 19,99 or ,99 -> decimal comma
            n = n.replace(",", ".")
        else:
            # 14,400 -> thousands separator
            n = n.replace(",", "")

    try:
        numeric = float(n)
    except ValueError:
        numeric = None

    return numeric, symbol


def _extract_iaps_from_json(data: Any) -> List[AppleIAPItem]:
    """Recursively search parsed JSON structure for In-App Purchases section items."""
    found_items: List[AppleIAPItem] = []

    def _traverse(node: Any) -> None:
        if isinstance(node, dict):
            title = str(node.get("title", "")).strip().lower()
            section_id = str(node.get("id", "")).strip().lower()

            is_iap_section = (
                "in-app purchase" in title
                or "in app purchase" in title
                or title == "iap"
                or section_id == "in-app-purchases"
            )

            if is_iap_section and "items" in node and isinstance(node["items"], list):
                for item in node["items"]:
                    if isinstance(item, dict):
                        name = (
                            item.get("leadingText")
                            or item.get("name")
                            or item.get("title")
                            or ""
                        )
                        price_str = (
                            item.get("trailingText")
                            or item.get("price")
                            or item.get("priceFormatted")
                            or ""
                        )
                        if name and price_str:
                            num, sym = parse_price_and_currency(str(price_str))
                            found_items.append(
                                AppleIAPItem(
                                    name=str(name).strip(),
                                    local_price_str=str(price_str).strip(),
                                    numeric_price=num,
                                    currency_symbol=sym,
                                )
                            )
                return

            for v in node.values():
                _traverse(v)
        elif isinstance(node, list):
            for elem in node:
                _traverse(elem)

    _traverse(data)
    return found_items


def _extract_iaps_from_html_regex(html_text: str) -> List[AppleIAPItem]:
    """Fallback regex extractor for Apple In-App Purchases when JSON script tag is missing."""
    found: List[AppleIAPItem] = []
    if not html_text:
        return found

    # 1. Search within In-App Purchases container
    search_scope = html_text
    lower_html = html_text.lower()
    if "</section>" in lower_html and "in-app-purchases" in lower_html:
        section_match = re.search(
            r'<section[^>]*class="[^"]*in-app-purchases[^"]*"[^>]*>((?:(?!<section\b).)*?)</section>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if section_match:
            search_scope = section_match.group(1)
    elif "</ol>" in lower_html and "list--in-app-purchases" in lower_html:
        section_match = re.search(
            r'<ol[^>]*class="[^"]*list--in-app-purchases[^"]*"[^>]*>((?:(?!<ol\b).)*?)</ol>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if section_match:
            search_scope = section_match.group(1)

    # Extract standard list items scoped to individual <li>...</li> tags
    if "</li>" in search_scope.lower() and "</span>" in search_scope.lower():
        li_pattern = re.compile(r"<li\b[^>]*>((?:(?!<li\b).)*?)</li>", re.DOTALL | re.IGNORECASE)
        title_pattern = re.compile(
            r'<span[^>]*class="[^"]*list-item__title[^"]*"[^>]*>(.*?)</span>',
            re.DOTALL | re.IGNORECASE,
        )
        price_pattern = re.compile(
            r'<span[^>]*class="[^"]*list-item__price[^"]*"[^>]*>(.*?)</span>',
            re.DOTALL | re.IGNORECASE,
        )

        for li_match in li_pattern.finditer(search_scope):
            li_content = li_match.group(1)
            if "</span>" not in li_content.lower():
                continue
            m_title = title_pattern.search(li_content)
            m_price = price_pattern.search(li_content)
            if m_title and m_price:
                raw_name = html.unescape(re.sub(r"<[^>]+>", "", m_title.group(1))).strip()
                raw_price = html.unescape(re.sub(r"<[^>]+>", "", m_price.group(1))).strip()
                if raw_name and raw_price:
                    num, sym = parse_price_and_currency(raw_price)
                    found.append(
                        AppleIAPItem(
                            name=raw_name,
                            local_price_str=raw_price,
                            numeric_price=num,
                            currency_symbol=sym,
                        )
                    )

    if found:
        return found

    # 2. TextPair JSON regex if script tag id was stripped or unquoted
    textpair_pattern = re.compile(
        r'["\']leadingText["\']\s*:\s*["\']([^"\']+)["\']\s*,\s*["\']trailingText["\']\s*:\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in textpair_pattern.finditer(html_text):
        raw_name = html.unescape(m.group(1)).strip()
        raw_price = html.unescape(m.group(2)).strip()
        num, sym = parse_price_and_currency(raw_price)
        if num is not None or sym is not None:
            found.append(
                AppleIAPItem(
                    name=raw_name,
                    local_price_str=raw_price,
                    numeric_price=num,
                    currency_symbol=sym,
                )
            )

    return found


class AppleStorefrontValidator:
    """Validates Apple App Store storefronts, checks anti-spoofing redirects, and parses IAPs."""

    KNOWN_APP_IDS: Dict[str, str] = dict(KNOWN_APP_IDS)

    def __init__(self, client: Optional[HttpClient] = None) -> None:
        self.client = client

    def _get_client(self) -> HttpClient:
        if self.client is not None:
            return self.client
        self.client = HttpClient()
        return self.client

    def parse_apple_iap_html(self, html_text: str) -> List[AppleIAPItem]:
        """Parse Apple App Store SSR HTML for In-App Purchase tiers.

        Extracts <script id="serialized-server-data"> JSON structure with fallback
        to regex parsing of server-rendered DOM elements.
        """
        if not html_text or not isinstance(html_text, str) or not html_text.strip():
            return []

        # 1. Search for serialized server data script tag
        json_matches = re.findall(
            r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        for content in json_matches:
            content_clean = content.strip()
            if not content_clean:
                continue
            try:
                data = json.loads(content_clean)
                items = _extract_iaps_from_json(data)
                if items:
                    return items
            except Exception:
                pass

        # 2. Robust fallback regex extraction
        return _extract_iaps_from_html_regex(html_text)

    # Alias for architecture analysis compatibility
    parse_iaps_from_ssr = parse_apple_iap_html

    def validate_product(
        self,
        product_slug: str,
        country_code: str,
    ) -> AppleValidationResult:
        """Validate product availability on regional Apple Storefront.

        Constructs Apple Store URL, checks CDN redirect anti-spoofing (detecting
        fallback redirects to US store), catches 4xx fast-fail errors immediately
        without retry delays, and extracts In-App Purchase pricing.
        """
        slug_raw = (product_slug or "").strip()
        slug_norm = slug_raw.lower().strip("/")
        if "/" in slug_norm:
            slug_norm = slug_norm.split("/")[-1]

        app_id = self.KNOWN_APP_IDS.get(slug_norm)
        if not app_id:
            m_id = re.search(r"id(\d+)", slug_raw)
            if m_id:
                app_id = m_id.group(0)
            else:
                return AppleValidationResult(
                    product_slug=slug_raw,
                    country_code=country_code,
                    is_spoofed=False,
                    is_valid=False,
                    error=f"Unknown product slug '{product_slug}': no known Apple App ID mapping",
                    error_message=f"Unknown product slug '{product_slug}': no known Apple App ID mapping",
                )

        formatted_app_id = app_id if app_id.startswith("id") else f"id{app_id}"
        country_clean = (country_code or "").strip().lower()
        requested_url = f"https://apps.apple.com/{country_clean}/app/{slug_norm}/{formatted_app_id}"

        client = self._get_client()

        try:
            status_code, final_url, body_bytes, is_spoofed = client.fetch_with_redirect_check(
                requested_url, expected_country=country_clean
            )
        except HttpFastFailError as e:
            return AppleValidationResult(
                product_slug=slug_raw,
                country_code=country_code,
                requested_url=requested_url,
                final_url=getattr(e, "url", requested_url),
                status_code=e.status_code,
                is_spoofed=False,
                is_valid=False,
                app_id=formatted_app_id,
                error=f"App not available in storefront '{country_code}' (HTTP {e.status_code})",
                error_message=f"App not available in storefront '{country_code}' (HTTP {e.status_code})",
            )
        except (NetworkError, HttpError) as e:
            status = getattr(e, "status_code", 500)
            return AppleValidationResult(
                product_slug=slug_raw,
                country_code=country_code,
                requested_url=requested_url,
                final_url=requested_url,
                status_code=status,
                is_spoofed=False,
                is_valid=False,
                app_id=formatted_app_id,
                error=f"Network error accessing Apple storefront: {e}",
                error_message=f"Network error accessing Apple storefront: {e}",
            )

        if is_spoofed:
            return AppleValidationResult(
                product_slug=slug_raw,
                country_code=country_code,
                requested_url=requested_url,
                final_url=final_url,
                status_code=status_code,
                is_spoofed=True,
                is_valid=False,
                app_id=formatted_app_id,
                error=f"CDN redirect anti-spoofing triggered: requested country '{country_code}' was redirected to '{final_url}'",
                error_message=f"CDN redirect anti-spoofing triggered: requested country '{country_code}' was redirected to '{final_url}'",
                items=[],
            )

        if status_code >= 400:
            return AppleValidationResult(
                product_slug=slug_raw,
                country_code=country_code,
                requested_url=requested_url,
                final_url=final_url,
                status_code=status_code,
                is_spoofed=False,
                is_valid=False,
                app_id=formatted_app_id,
                error=f"App not available in storefront '{country_code}' (HTTP {status_code})",
                error_message=f"App not available in storefront '{country_code}' (HTTP {status_code})",
                items=[],
            )

        # Follow in-country canonical 301/302 redirects when is_spoofed is False (up to 3 hops)
        hops = 0
        max_hops = 3
        current_url = final_url
        while status_code in (301, 302, 303, 307, 308) and current_url and hops < max_hops:
            hops += 1
            followed = False
            # First try traversal via fetch_with_redirect_check to preserve per-hop anti-spoofing verification
            if hasattr(client, "fetch_with_redirect_check"):
                try:
                    n_code, n_final, n_body, n_spoofed = client.fetch_with_redirect_check(
                        current_url, expected_country=country_clean
                    )
                    if n_spoofed:
                        return AppleValidationResult(
                            product_slug=slug_raw,
                            country_code=country_code,
                            requested_url=requested_url,
                            final_url=n_final,
                            status_code=n_code,
                            is_spoofed=True,
                            is_valid=False,
                            app_id=formatted_app_id,
                            error=f"CDN redirect anti-spoofing triggered: requested country '{country_code}' was redirected to '{n_final}'",
                            error_message=f"CDN redirect anti-spoofing triggered: requested country '{country_code}' was redirected to '{n_final}'",
                            items=[],
                        )
                    if n_code < 400 and n_body and n_code not in (301, 302, 303, 307, 308):
                        status_code = n_code
                        final_url = n_final
                        body_bytes = n_body
                        followed = True
                        break
                    elif n_code in (301, 302, 303, 307, 308) and n_final and n_final != current_url:
                        current_url = n_final
                        final_url = n_final
                        status_code = n_code
                        followed = True
                        continue
                except Exception:
                    pass

            if not followed:
                if hasattr(client, "get"):
                    try:
                        dest_code, _, dest_body = client.get(current_url or final_url, follow_redirects=True)
                        if dest_code < 400 and dest_body:
                            status_code = dest_code
                            body_bytes = dest_body
                            break
                    except Exception:
                        pass
                elif hasattr(client, "fetch_bytes"):
                    try:
                        dest_body = client.fetch_bytes(current_url or final_url)
                        if dest_body:
                            status_code = 200
                            body_bytes = dest_body
                            break
                    except Exception:
                        pass
                break

        html_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
        items = self.parse_apple_iap_html(html_text)

        return AppleValidationResult(
            product_slug=slug_raw,
            country_code=country_code,
            requested_url=requested_url,
            final_url=final_url,
            status_code=status_code,
            is_spoofed=False,
            is_valid=True,
            app_id=formatted_app_id,
            items=items,
            error=None,
            error_message=None,
        )

    # Alias for architecture analysis compatibility
    validate_country = validate_product

    def cross_validate_pricing(
        self,
        pricing_result: ProductPricingResult,
        country_code: str,
    ) -> Tuple[bool, Optional[str]]:
        """Cross-validate OpenTheRank extracted price against official Apple IAP tier price.

        Verifies currency and price parity consistency. Returns (is_valid, error_reason).
        """
        if not pricing_result or not pricing_result.product:
            return False, "Invalid pricing result: missing product information"

        val_res = self.validate_product(pricing_result.product.slug, country_code)

        if val_res.is_spoofed:
            err = f"Anti-spoofing alert: Storefront '{country_code}' was redirected to '{val_res.final_url}'"
            pricing_result.first_party_validated = False
            pricing_result.validation_source = "apple"
            if err not in pricing_result.validation_notes:
                pricing_result.validation_notes.append(err)
            return False, err

        if not val_res.is_valid or val_res.error:
            err = f"Apple validation failed: {val_res.error or f'HTTP {val_res.status_code}'}"
            pricing_result.first_party_validated = False
            pricing_result.validation_source = "apple"
            if err not in pricing_result.validation_notes:
                pricing_result.validation_notes.append(err)
            return False, err

        if not val_res.items:
            err = f"No In-App Purchases found on Apple storefront for country '{country_code}'"
            pricing_result.first_party_validated = False
            pricing_result.validation_source = "apple"
            if err not in pricing_result.validation_notes:
                pricing_result.validation_notes.append(err)
            return False, err

        target_cc = country_code.strip().upper()
        records = [r for r in pricing_result.records if r.country_code.upper() == target_cc]
        if not records:
            err = f"No pricing record found in OpenTheRank result for country '{country_code}'"
            pricing_result.first_party_validated = False
            pricing_result.validation_source = "apple"
            if err not in pricing_result.validation_notes:
                pricing_result.validation_notes.append(err)
            return False, err

        # Prefer appstore platform record
        rec = next((r for r in records if r.platform.lower() in ("appstore", "apple")), records[0])

        otr_tiers = [rec.primary_tier] + list(rec.additional_tiers.values())

        matched_pair = None
        # Pass 1: Strict exact name match
        for o_tier in otr_tiers:
            o_name = o_tier.name.strip().lower()
            for a_item in val_res.items:
                a_name = a_item.name.strip().lower()
                if o_name == a_name:
                    matched_pair = (o_tier, a_item)
                    break
            if matched_pair:
                break

        # Pass 2: Specific plan/tier keyword match (e.g., "Plus", "Pro") rather than generic product tokens like "ChatGPT"
        if not matched_pair:
            TIER_TOKENS = {
                "plus", "pro", "team", "max", "ultra", "premium",
                "family", "individual", "student", "standard", "basic",
                "vip", "mini", "starter", "ultimate", "enterprise",
                "duo", "personal", "lite", "turbo", "annual", "monthly", "yearly",
            }
            # Identify generic product tokens to exclude from tier keyword matching
            generic_tokens = set()
            if pricing_result.product:
                if pricing_result.product.name:
                    generic_tokens.update(re.findall(r"\w+", pricing_result.product.name.lower()))
                if pricing_result.product.slug:
                    generic_tokens.update(re.findall(r"\w+", pricing_result.product.slug.lower()))
            generic_tokens.update({"app", "official", "mobile", "ios", "edition", "subscription", "plan", "the", "for"})

            best_score = 0.0
            best_pair = None

            for o_tier in otr_tiers:
                o_words = set(re.findall(r"\w+", o_tier.name.lower()))
                # Specific tier tokens: either in TIER_TOKENS or non-generic tokens
                o_tier_tokens = (o_words & TIER_TOKENS) | (o_words - generic_tokens)
                for a_item in val_res.items:
                    a_words = set(re.findall(r"\w+", a_item.name.lower()))
                    a_tier_tokens = (a_words & TIER_TOKENS) | (a_words - generic_tokens)

                    # Both must share specific tier keyword(s)
                    shared_tier_tokens = o_tier_tokens & a_tier_tokens
                    if not shared_tier_tokens:
                        continue

                    # If both have known tier tokens and they conflict (e.g. Plus vs Pro), skip
                    o_known = o_words & TIER_TOKENS
                    a_known = a_words & TIER_TOKENS
                    if o_known and a_known and o_known != a_known:
                        continue

                    o_name = o_tier.name.strip().lower()
                    a_name = a_item.name.strip().lower()
                    is_sub = o_name in a_name or a_name in o_name

                    score = float(len(shared_tier_tokens))
                    if o_known and a_known and o_known == a_known:
                        score += 5.0
                    if is_sub:
                        score += 1.0
                    if o_tier is rec.primary_tier:
                        score += 0.5

                    if score > best_score:
                        best_score = score
                        best_pair = (o_tier, a_item)

            if best_pair and best_score > 0.0:
                matched_pair = best_pair

        # Fallback pass: word overlap fallback only if no exact/specific match
        if not matched_pair:
            best_overlap = 0.0
            best_pair = None
            for o_tier in otr_tiers:
                o_words = set(re.findall(r"\w+", o_tier.name.lower()))
                for a_item in val_res.items:
                    a_words = set(re.findall(r"\w+", a_item.name.lower()))
                    overlap = len(o_words & a_words)
                    if overlap > 0:
                        score = float(overlap)
                        if o_tier is rec.primary_tier:
                            score += 0.5
                        if score > best_overlap:
                            best_overlap = score
                            best_pair = (o_tier, a_item)
            if best_pair:
                matched_pair = best_pair

        if not matched_pair:
            matched_pair = (rec.primary_tier, val_res.items[0])

        otr_tier, apple_item = matched_pair
        otr_num, otr_sym = parse_price_and_currency(otr_tier.local_price_str)

        # 1. Currency symbol consistency check
        if otr_sym and apple_item.currency_symbol:
            sym_o = otr_sym.strip()
            sym_a = apple_item.currency_symbol.strip()
            if sym_o != sym_a and sym_o not in sym_a and sym_a not in sym_o:
                err = f"Currency mismatch for {otr_tier.name}: OpenTheRank has '{sym_o}', Apple has '{sym_a}'"
                pricing_result.first_party_validated = False
                pricing_result.validation_source = "apple"
                if err not in pricing_result.validation_notes:
                    pricing_result.validation_notes.append(err)
                return False, err

        # 2. Numeric price parity check
        if otr_num is not None and apple_item.numeric_price is not None:
            abs_diff = abs(otr_num - apple_item.numeric_price)
            tolerance = max(1.0, 0.05 * apple_item.numeric_price)
            if abs_diff > tolerance:
                err = (
                    f"Price parity mismatch for {otr_tier.name}: "
                    f"OpenTheRank is {otr_tier.local_price_str} ({otr_num}), "
                    f"Apple IAP is {apple_item.local_price_str} ({apple_item.numeric_price})"
                )
                pricing_result.first_party_validated = False
                pricing_result.validation_source = "apple"
                if err not in pricing_result.validation_notes:
                    pricing_result.validation_notes.append(err)
                return False, err

        pricing_result.first_party_validated = True
        pricing_result.validation_source = "apple"
        note = f"Parity verified with Apple Storefront ({country_code}): {apple_item.name} at {apple_item.local_price_str}"
        if note not in pricing_result.validation_notes:
            pricing_result.validation_notes.append(note)
        return True, None


_default_validator = AppleStorefrontValidator()


def parse_apple_iap_html(html_text: str) -> List[AppleIAPItem]:
    """Module-level function parsing Apple App Store IAP data from HTML."""
    return _default_validator.parse_apple_iap_html(html_text)


def validate_product(product_slug: str, country_code: str) -> AppleValidationResult:
    """Module-level function validating product storefront with redirect anti-spoofing."""
    return _default_validator.validate_product(product_slug, country_code)


def cross_validate_pricing(
    pricing_result: ProductPricingResult,
    country_code: str,
) -> Tuple[bool, Optional[str]]:
    """Module-level function cross-validating OpenTheRank pricing with Apple IAP parity."""
    return _default_validator.cross_validate_pricing(pricing_result, country_code)


__all__ = [
    "KNOWN_APP_IDS",
    "AppleStorefrontValidator",
    "parse_apple_iap_html",
    "validate_product",
    "cross_validate_pricing",
    "parse_price_and_currency",
]
