"""Multi-Platform & Multi-Region Pricing Table Extractor and Arbitrage Engine."""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple, Union

from openrank.models import (
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

COUNTRY_NAME_TO_CODE: Dict[str, str] = {
    "united states": "US", "usa": "US", "us": "US",
    "philippines": "PH", "canada": "CA", "pakistan": "PK",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "india": "IN", "finland": "FI", "turkey": "TR", "türkiye": "TR",
    "argentina": "AR", "brazil": "BR", "nigeria": "NG",
    "japan": "JP", "hong kong": "HK", "united arab emirates": "AE", "uae": "AE",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "australia": "AU", "new zealand": "NZ", "mexico": "MX",
    "south korea": "KR", "korea": "KR", "singapore": "SG",
    "indonesia": "ID", "malaysia": "MY", "thailand": "TH",
    "vietnam": "VN", "taiwan": "TW", "china": "CN",
    "colombia": "CO", "chile": "CL", "peru": "PE",
    "south africa": "ZA", "egypt": "EG", "poland": "PL",
    "sweden": "SE", "norway": "NO", "denmark": "DK",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "ireland": "IE", "portugal": "PT",
    "czech republic": "CZ", "czechia": "CZ", "greece": "GR",
    "hungary": "HU", "romania": "RO", "ukraine": "UA",
}

CODE_TO_COUNTRY_NAME: Dict[str, str] = {
    "US": "United States", "PH": "Philippines", "CA": "Canada",
    "PK": "Pakistan", "GB": "United Kingdom", "IN": "India",
    "FI": "Finland", "TR": "Turkey", "AR": "Argentina",
    "BR": "Brazil", "NG": "Nigeria", "JP": "Japan",
    "HK": "Hong Kong", "AE": "United Arab Emirates", "DE": "Germany",
    "FR": "France", "IT": "Italy", "ES": "Spain",
    "AU": "Australia", "NZ": "New Zealand", "MX": "Mexico",
    "KR": "South Korea", "SG": "Singapore", "ID": "Indonesia",
    "MY": "Malaysia", "TH": "Thailand", "VN": "Vietnam",
    "TW": "Taiwan", "CN": "China", "CO": "Colombia",
    "CL": "Chile", "PE": "Peru", "ZA": "South Africa",
    "EG": "Egypt", "PL": "Poland", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "NL": "Netherlands",
    "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "IE": "Ireland", "PT": "Portugal", "CZ": "Czech Republic",
    "GR": "Greece", "HU": "Hungary", "RO": "Romania",
    "UA": "Ukraine",
}


class HTMLNode:
    """Lightweight DOM node representing parsed HTML elements."""

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional[HTMLNode] = None) -> None:
        self.tag = tag.lower()
        self.attrs = {k.lower(): v for k, v in attrs.items()}
        self.children: List[Union[HTMLNode, str]] = []
        self.parent = parent

    def text(self) -> str:
        """Collect and unescape all recursive text children."""
        parts: List[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif isinstance(child, HTMLNode):
                parts.append(child.text())
        joined = " ".join("".join(parts).split())
        return html.unescape(joined)

    def find_all(self, tag: Optional[str] = None, **kwargs: Any) -> List[HTMLNode]:
        """Recursively search for matching descendant nodes."""
        results: List[HTMLNode] = []
        target_tag = tag.lower() if tag else None

        for child in self.children:
            if isinstance(child, HTMLNode):
                match = True
                if target_tag and child.tag != target_tag:
                    match = False
                for k, v in kwargs.items():
                    attr_key = "class" if k.lower() in ("class_", "class") else k.lower()
                    attr_val = child.attrs.get(attr_key, "")
                    if attr_key == "class":
                        classes = attr_val.split()
                        if isinstance(v, str) and v not in classes:
                            match = False
                    elif attr_val != v:
                        match = False
                if match:
                    results.append(child)
                results.extend(child.find_all(tag, **kwargs))
        return results

    def find(self, tag: Optional[str] = None, **kwargs: Any) -> Optional[HTMLNode]:
        """Return first matching descendant node or None."""
        nodes = self.find_all(tag, **kwargs)
        return nodes[0] if nodes else None


class HTMLTreeParser(HTMLParser):
    """Zero-dependency HTMLParser that builds a queryable DOM tree."""

    SELF_CLOSING = {"img", "br", "hr", "input", "meta", "link", "source", "use", "col", "path"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HTMLNode("root", {})
        self.stack: List[HTMLNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag_lower = tag.lower()
        attrs_dict = {k: v for k, v in attrs if v is not None}
        parent = self.stack[-1]
        node = HTMLNode(tag_lower, attrs_dict, parent=parent)
        parent.children.append(node)
        if tag_lower not in self.SELF_CLOSING:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag_lower:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1].children.append(data)


def _parse_usd_price(raw: Any) -> float:
    """Parse numeric USD float from string or attribute."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            pass
    return 0.0


def _parse_delta_percent(raw: Any) -> Optional[float]:
    """Parse percentage delta float from string or attribute."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).replace("%", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            pass
    return None


def _parse_risk_level(raw: str) -> str:
    """Normalize payment risk to 'Low', 'Medium', or 'High'."""
    r = raw.strip().lower()
    if "high" in r:
        return "High"
    if "med" in r:
        return "Medium"
    if "low" in r:
        return "Low"
    return "Low"


class PricingEngine:
    """Extracts regional pricing, subscription tiers, and currency arbitrage from OpenTheRank."""

    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        from openrank.client import HttpClient

        self.client = HttpClient()
        return self.client

    def fetch_pricing(
        self,
        product: ProductSummary,
        platform: str = "all",
        country: Optional[str] = None,
    ) -> ProductPricingResult:
        """Fetch live OpenTheRank HTML and parse regional pricing tables."""
        client = self._get_client()
        url = product.url
        if not url:
            url = f"https://opentherank.com{product.path}"

        if hasattr(client, "fetch_bytes"):
            raw_bytes = client.fetch_bytes(url)
        elif hasattr(client, "get"):
            _, _, raw_bytes = client.get(url)
        else:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "openrank/0.1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_bytes = resp.read()

        html_text = raw_bytes.decode("utf-8", errors="replace")
        return self.parse_pricing_html(
            html_text=html_text,
            product=product,
            platform=platform,
            country=country,
        )

    # Alias for architecture compatibility
    get_pricing = fetch_pricing

    def parse_pricing_html(
        self,
        html_text: str,
        product: ProductSummary,
        platform: str = "all",
        country: Optional[str] = None,
    ) -> ProductPricingResult:
        """Parse server-rendered HTML tables into structured ProductPricingResult."""
        if not html_text or not isinstance(html_text, str) or not html_text.strip():
            return ProductPricingResult(
                product=product,
                records=[],
                baseline_us_price=None,
                platforms=[],
            )

        parser = HTMLTreeParser()
        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            # On parser error, return empty gracefully
            return ProductPricingResult(
                product=product,
                records=[],
                baseline_us_price=None,
                platforms=[],
            )

        tables = parser.root.find_all("table")
        if not tables:
            return ProductPricingResult(
                product=product,
                records=[],
                baseline_us_price=None,
                platforms=[],
            )

        # Check for page-level or header-level baseline price hint
        page_baseline: Optional[float] = None
        header_nodes = parser.root.find_all(class_="product-header")
        for h in header_nodes:
            if "data-us-baseline" in h.attrs:
                val = _parse_usd_price(h.attrs["data-us-baseline"])
                if val > 0:
                    page_baseline = val
                    break
        if page_baseline is None:
            m_us = re.search(r"US Baseline:\s*\$([0-9]+(?:\.[0-9]+)?)", html_text, re.IGNORECASE)
            if m_us:
                try:
                    page_baseline = float(m_us.group(1))
                except ValueError:
                    pass

        all_records: List[RegionPricingRecord] = []
        platforms_found: List[str] = []

        for t_idx, table in enumerate(tables):
            table_platform = self._determine_table_platform(table, t_idx)
            if table_platform not in platforms_found:
                platforms_found.append(table_platform)

            records = self._parse_table_rows(table, table_platform)
            all_records.extend(records)

        # Baseline resolution
        baseline_us_price: Optional[float] = None
        for r in all_records:
            if r.country_code.upper() == "US":
                baseline_us_price = r.primary_tier.usd_price
                break

        if baseline_us_price is None and page_baseline is not None:
            baseline_us_price = page_baseline

        # Compute dynamic deltas if missing and baseline is available
        if baseline_us_price is not None and baseline_us_price > 0:
            for r in all_records:
                if r.vs_us_delta is None:
                    calc_delta = round(((r.primary_tier.usd_price - baseline_us_price) / baseline_us_price) * 100, 1)
                    r.vs_us_delta = calc_delta
                if r.primary_tier.delta_percent is None:
                    r.primary_tier.delta_percent = r.vs_us_delta

        # Apply filtering
        filtered_records = self._filter_records(
            records=all_records,
            platform_filter=platform,
            country_filter=country,
        )

        return ProductPricingResult(
            product=product,
            records=filtered_records,
            baseline_us_price=baseline_us_price,
            platforms=platforms_found,
            us_baseline_usd=baseline_us_price,
        )

    def parse_html_tables(self, html: str, product: ProductSummary) -> List[RegionPricingRecord]:
        """Convenience method returning parsed RegionPricingRecord list without filtering."""
        result = self.parse_pricing_html(html, product=product, platform="all")
        return result.records

    def _determine_table_platform(self, table: HTMLNode, t_idx: int) -> str:
        """Determine platform context ('appstore', 'play', 'web', 'direct') for a table."""
        # 1. Attribute check on table
        data_p = table.attrs.get("data-platform", "").strip().lower()
        if data_p:
            return self._normalize_platform(data_p)

        t_id = table.attrs.get("id", "").lower()
        t_cls = table.attrs.get("class", "").lower()
        combined = f"{t_id} {t_cls}"

        if "appstore" in combined or "apple" in combined:
            return "appstore"
        if "play" in combined or "googleplay" in combined:
            return "play"
        if "direct" in combined:
            return "direct"
        if "web" in combined:
            return "web"

        # 2. Check enclosing parent or previous headings
        curr = table.parent
        while curr and curr.tag != "root":
            section_id = curr.attrs.get("id", "").lower()
            if "appstore" in section_id:
                return "appstore"
            if "play" in section_id:
                return "play"
            if "direct" in section_id:
                return "direct"
            if "web" in section_id:
                return "web"

            # Check headings in parent
            for h in curr.find_all("h2") + curr.find_all("h3") + curr.find_all("h4"):
                h_text = h.text().lower()
                if "app store" in h_text:
                    return "appstore"
                if "google play" in h_text or "play store" in h_text:
                    return "play"
                if "direct" in h_text or "hardware" in h_text:
                    return "direct"
                if "web" in h_text:
                    return "web"
            curr = curr.parent

        # 3. Default index fallback: Table 0 = App Store, Table 1 = Google Play
        if t_idx == 0:
            return "appstore"
        if t_idx == 1:
            return "play"
        return "web"

    @staticmethod
    def _normalize_platform(p: str) -> str:
        clean = p.strip().lower()
        if clean in ("appstore", "app_store", "apple", "app store"):
            return "appstore"
        if clean in ("play", "googleplay", "google_play", "playstore", "play store", "google"):
            return "play"
        if clean in ("web", "direct", "website"):
            return clean
        return clean

    def _parse_table_rows(self, table: HTMLNode, platform: str) -> List[RegionPricingRecord]:
        """Extract regional pricing records from table rows."""
        records: List[RegionPricingRecord] = []

        # Parse column headers
        header_nodes = table.find_all("th")
        col_names: List[str] = [h.text().strip() for h in header_nodes]

        # Identify tier columns and other column roles
        tier_cols: List[Tuple[int, str]] = []
        delta_col_idx: Optional[int] = None
        tax_col_idx: Optional[int] = None
        risk_col_idx: Optional[int] = None
        cc_col_idx: Optional[int] = None
        region_col_idx: Optional[int] = None

        non_tier_headers = {
            "#", "rank", "country", "region", "flag", "cc",
            "vs us %", "vs us", "delta", "% diff", "discount",
            "tax", "vat", "tax info",
            "risk", "payment risk", "risk level",
        }

        for idx, col in enumerate(col_names):
            col_lower = col.lower()
            if col_lower in ("#", "rank"):
                pass
            elif col_lower in ("cc", "country code"):
                cc_col_idx = idx
            elif col_lower in ("country", "region"):
                region_col_idx = idx
            elif any(k in col_lower for k in ("vs us", "delta", "diff")):
                delta_col_idx = idx
            elif "tax" in col_lower or "vat" in col_lower:
                tax_col_idx = idx
            elif "risk" in col_lower:
                risk_col_idx = idx
            elif col_lower not in non_tier_headers and col:
                # Potential tier column (e.g. Plus, Pro 5x, Basic, Standard, Premium, 256GB, 512GB, 1TB)
                tier_cols.append((idx, col))

        # Parse data rows
        rows = table.find_all("tr")
        for tr in rows:
            td_cells = tr.find_all("td")
            if not td_cells:
                continue

            # 1. Extract Country Code
            cc = self._extract_country_code(tr, td_cells, cc_col_idx)
            if not cc:
                continue

            # 2. Extract Region Name
            region_name = self._extract_region_name(tr, td_cells, region_col_idx, cc)

            # 3. Extract Price Tiers
            primary_tier, additional_tiers = self._extract_tiers(tr, td_cells, tier_cols)
            if not primary_tier:
                continue

            # 4. Extract Delta %
            delta = self._extract_delta(tr, td_cells, delta_col_idx)

            # 5. Extract Tax Info
            tax_info = self._extract_tax(tr, td_cells, tax_col_idx)

            # 6. Extract Risk Level and Note
            risk_level, risk_note = self._extract_risk(tr, td_cells, risk_col_idx)

            # 7. Snapshot date
            snapshot_date = tr.attrs.get("data-snapshot", "")

            records.append(
                RegionPricingRecord(
                    country_code=cc,
                    region_name=region_name,
                    platform=platform,
                    primary_tier=primary_tier,
                    additional_tiers=additional_tiers,
                    vs_us_delta=delta,
                    tax_info=tax_info,
                    risk_level=risk_level,
                    risk_note=risk_note,
                    snapshot_date=snapshot_date,
                )
            )

        return records

    def _extract_country_code(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        cc_col_idx: Optional[int],
    ) -> Optional[str]:
        """Extract 2-letter ISO country code from row attributes, SVG uses, or classes."""
        # 1. data-cc attribute on tr
        if "data-cc" in tr.attrs:
            cc = tr.attrs["data-cc"].strip().upper()
            if len(cc) == 2:
                return cc

        # 2. data-cc attribute on any child cell
        for td in td_cells:
            if "data-cc" in td.attrs:
                cc = td.attrs["data-cc"].strip().upper()
                if len(cc) == 2:
                    return cc

        # 3. SVG <use href="#f-PH">
        uses = tr.find_all("use")
        for u in uses:
            href = u.attrs.get("href") or u.attrs.get("xlink:href") or ""
            m = re.search(r"#f-([A-Za-z]{2})\b", href)
            if m:
                return m.group(1).upper()

        # 4. Flag classes like fi-ph or flag-ph
        for el in tr.find_all("span") + tr.find_all("i") + tr.find_all("svg"):
            cls_str = el.attrs.get("class", "")
            m = re.search(r"\b(?:fi|flag)-([a-zA-Z]{2})\b", cls_str)
            if m:
                return m.group(1).upper()

        # 5. Cell in cc_col_idx
        if cc_col_idx is not None and cc_col_idx < len(td_cells):
            txt = td_cells[cc_col_idx].text().strip().upper()
            if len(txt) == 2 and txt.isalpha():
                return txt

        # 6. Lookup from region link text
        r_link = tr.find(class_="ctl-region-link")
        if r_link:
            r_name = r_link.text().strip().lower()
            if r_name in COUNTRY_NAME_TO_CODE:
                return COUNTRY_NAME_TO_CODE[r_name]

        return None

    def _extract_region_name(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        region_col_idx: Optional[int],
        country_code: str,
    ) -> str:
        """Extract display region/country name."""
        r_link = tr.find(class_="ctl-region-link")
        if r_link:
            return r_link.text().strip()

        if region_col_idx is not None and region_col_idx < len(td_cells):
            txt = td_cells[region_col_idx].text().strip()
            # Clean possible rank or country code from text
            txt_clean = re.sub(r"^[0-9]+\s*", "", txt).strip()
            if txt_clean:
                return txt_clean

        # Fallback to known mapping
        return CODE_TO_COUNTRY_NAME.get(country_code, country_code)

    def _extract_tiers(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        tier_cols: List[Tuple[int, str]],
    ) -> Tuple[Optional[PriceTier], Dict[str, PriceTier]]:
        """Extract primary PriceTier and additional_tiers dictionary."""
        extracted: List[PriceTier] = []

        if tier_cols:
            for idx, col_name in tier_cols:
                if idx < len(td_cells):
                    cell = td_cells[idx]
                    local_price = ""
                    p_elem = cell.find(class_="ctl-price-local")
                    if p_elem:
                        local_price = p_elem.text().strip()
                    else:
                        local_price = cell.text().strip()

                    usd_price = 0.0
                    if "data-otr-usd" in cell.attrs:
                        usd_price = _parse_usd_price(cell.attrs["data-otr-usd"])
                    else:
                        usd_price = _parse_usd_price(local_price)

                    tier_name = cell.attrs.get("data-plan") or col_name
                    if usd_price > 0 or local_price:
                        extracted.append(
                            PriceTier(
                                name=tier_name,
                                local_price_str=local_price,
                                usd_price=usd_price,
                            )
                        )
        else:
            # Fallback: scan all cells for data-otr-usd or data-plan
            for cell in td_cells:
                if "data-otr-usd" in cell.attrs or "data-plan" in cell.attrs:
                    tier_name = cell.attrs.get("data-plan", "Standard")
                    usd_price = _parse_usd_price(cell.attrs.get("data-otr-usd", "0"))
                    p_elem = cell.find(class_="ctl-price-local")
                    local_price = p_elem.text().strip() if p_elem else cell.text().strip()
                    extracted.append(
                        PriceTier(
                            name=tier_name,
                            local_price_str=local_price,
                            usd_price=usd_price,
                        )
                    )

        if not extracted:
            return None, {}

        primary = extracted[0]
        additional: Dict[str, PriceTier] = {}
        for tier in extracted[1:]:
            additional[tier.name] = tier

        return primary, additional

    def _extract_delta(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        delta_col_idx: Optional[int],
    ) -> Optional[float]:
        """Extract delta percentage vs US baseline."""
        delta_el = tr.find(class_="ctl-delta")
        if delta_el:
            return _parse_delta_percent(delta_el.text())

        if delta_col_idx is not None and delta_col_idx < len(td_cells):
            return _parse_delta_percent(td_cells[delta_col_idx].text())

        for cell in td_cells:
            if "data-delta" in cell.attrs:
                return _parse_delta_percent(cell.attrs["data-delta"])

        return None

    def _extract_tax(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        tax_col_idx: Optional[int],
    ) -> str:
        """Extract tax handling notes."""
        tax_el = tr.find(class_="ctl-tax")
        if tax_el:
            return tax_el.text().strip()

        clamp_el = tr.find(class_="ctl-cell-clamp")
        if clamp_el:
            return clamp_el.text().strip()

        if tax_col_idx is not None and tax_col_idx < len(td_cells):
            return td_cells[tax_col_idx].text().strip()

        return ""

    def _extract_risk(
        self,
        tr: HTMLNode,
        td_cells: List[HTMLNode],
        risk_col_idx: Optional[int],
    ) -> Tuple[str, str]:
        """Extract payment risk level and explanatory note."""
        risk_level = "Low"
        risk_note = ""

        risk_el = tr.find(class_="ctl-risk")
        if risk_el:
            risk_level = _parse_risk_level(risk_el.text())

        note_el = tr.find(class_="ctl-risk-note")
        if note_el:
            risk_note = note_el.text().strip()

        if risk_level == "Low" and risk_col_idx is not None and risk_col_idx < len(td_cells):
            txt = td_cells[risk_col_idx].text().strip()
            risk_level = _parse_risk_level(txt)
            if not risk_note:
                # If note is combined in text
                m = re.search(r"\((.*?)\)", txt)
                if m:
                    risk_note = m.group(1).strip()

        return risk_level, risk_note

    def _filter_records(
        self,
        records: List[RegionPricingRecord],
        platform_filter: str,
        country_filter: Optional[str],
    ) -> List[RegionPricingRecord]:
        """Filter records by platform and strict 2-letter ISO country code."""
        filtered = list(records)

        # Platform filter
        p_clean = (platform_filter or "all").strip().lower()
        if p_clean != "all":
            if p_clean in ("appstore", "apple", "app_store"):
                filtered = [r for r in filtered if r.platform.lower() in ("appstore", "apple")]
            elif p_clean in ("play", "googleplay", "google_play", "playstore"):
                filtered = [r for r in filtered if r.platform.lower() in ("play", "googleplay")]
            elif p_clean in ("web", "direct", "website"):
                filtered = [r for r in filtered if r.platform.lower() in ("web", "direct")]
            else:
                filtered = [r for r in filtered if r.platform.lower() == p_clean]

        # Strict ISO2 country code filter
        if country_filter and country_filter.strip():
            c_clean = country_filter.strip().upper()
            filtered = [r for r in filtered if r.country_code.upper() == c_clean]

        return filtered


# Global alias and convenience functions
PricingExtractor = PricingEngine
_default_engine = PricingEngine()


def parse_pricing_html(
    html_text: str,
    product: ProductSummary,
    platform: str = "all",
    country: Optional[str] = None,
) -> ProductPricingResult:
    """Module-level function to parse SSR HTML tables."""
    return _default_engine.parse_pricing_html(
        html_text=html_text,
        product=product,
        platform=platform,
        country=country,
    )


def fetch_pricing(
    product: ProductSummary,
    platform: str = "all",
    country: Optional[str] = None,
) -> ProductPricingResult:
    """Module-level function to fetch and parse pricing tables."""
    return _default_engine.fetch_pricing(
        product=product,
        platform=platform,
        country=country,
    )


__all__ = [
    "PricingEngine",
    "PricingExtractor",
    "parse_pricing_html",
    "fetch_pricing",
    "HTMLNode",
    "HTMLTreeParser",
]
