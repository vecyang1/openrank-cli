"""Product catalog loader, search resolver, and multi-tier scorer."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from openrank.cache import CacheManager
from openrank.models import ProductSummary


class CatalogError(Exception):
    """Raised when catalog loading fails completely across all fallback tiers."""
    pass


class CatalogManager:
    """Discovers, caches, matches, and autocompletes products from OpenTheRank."""

    DEFAULT_CATALOG_URL = "https://opentherank.com/search-index.json"

    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
        client: Optional[Any] = None,
        default_ttl: int = 86400,
        catalog_url: str = DEFAULT_CATALOG_URL,
    ) -> None:
        self.cache = cache_manager or CacheManager(default_ttl=default_ttl)
        self.client = client
        self.default_ttl = default_ttl
        self.catalog_url = catalog_url
        self._in_memory_catalog: Optional[List[ProductSummary]] = None

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        from openrank.client import HttpClient

        self.client = HttpClient()
        return self.client

    def _parse_items(self, raw_data: Any) -> List[ProductSummary]:
        if not isinstance(raw_data, list):
            return []
        products: List[ProductSummary] = []
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("n") or item.get("name") or "").strip()
            path = str(item.get("h") or item.get("path") or "").strip()
            category = str(item.get("c") or item.get("category") or "").strip()
            if not name or not path:
                continue

            parts = [p for p in path.strip("/").split("/") if p]
            slug = parts[-1] if parts else name.lower().replace(" ", "-")
            url_path = path if path.startswith("/") else f"/{path}"
            url = f"https://opentherank.com{url_path}"

            products.append(
                ProductSummary(
                    name=name,
                    slug=slug,
                    path=path,
                    category=category,
                    url=url,
                )
            )
        return products

    def _load_bundled_catalog(self) -> Optional[list]:
        local_path = Path(__file__).resolve().parent / "data" / "default_catalog.json"
        if local_path.is_file():
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        try:
            import importlib.resources as pkg_resources

            if hasattr(pkg_resources, "files"):
                f_path = pkg_resources.files("openrank.data").joinpath("default_catalog.json")
                with f_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            elif hasattr(pkg_resources, "read_text"):
                text = pkg_resources.read_text("openrank.data", "default_catalog.json", encoding="utf-8")
                return json.loads(text)
        except Exception:
            pass
        return None

    def get_catalog(self, refresh: bool = False) -> List[ProductSummary]:
        """Load catalog following 4-tier fallback hierarchy:
        1. Fresh remote fetch (or when refresh=True or cache miss)
        2. Valid cache
        3. Bundled static default_catalog.json
        4. Stale (expired) cache
        """
        if not refresh and self._in_memory_catalog is not None:
            return self._in_memory_catalog

        # Tier 2: Valid Cache (when refresh=False)
        if not refresh:
            cached_data = self.cache.get("search-index", ignore_ttl=False)
            if cached_data:
                self._in_memory_catalog = self._parse_items(cached_data)
                return self._in_memory_catalog

        # Tier 1: Fresh Remote Fetch
        client = self._get_client()
        fetch_error: Optional[Exception] = None
        try:
            if hasattr(client, "fetch_bytes"):
                raw_bytes = client.fetch_bytes(self.catalog_url)
            elif hasattr(client, "get"):
                _, _, raw_bytes = client.get(self.catalog_url)
            else:
                import urllib.request

                req = urllib.request.Request(self.catalog_url, headers={"User-Agent": "openrank/0.1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw_bytes = resp.read()

            data = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(data, list) and len(data) > 0:
                try:
                    self.cache.set("search-index", data, ttl=self.default_ttl)
                except Exception:
                    pass
                self._in_memory_catalog = self._parse_items(data)
                return self._in_memory_catalog
        except Exception as e:
            fetch_error = e

        # Fallback Ladder on remote failure:
        # Tier 2: Valid Cache (if refresh was True but network failed)
        cached_data = self.cache.get("search-index", ignore_ttl=False)
        if cached_data:
            self._in_memory_catalog = self._parse_items(cached_data)
            return self._in_memory_catalog

        # Tier 3: Bundled Static Catalog
        bundled = self._load_bundled_catalog()
        if bundled:
            self._in_memory_catalog = self._parse_items(bundled)
            return self._in_memory_catalog

        # Tier 4: Stale Cache (expired)
        stale_data = self.cache.get("search-index", ignore_ttl=True)
        if stale_data:
            self._in_memory_catalog = self._parse_items(stale_data)
            return self._in_memory_catalog

        raise CatalogError(
            f"Failed to load product catalog: remote endpoint unreachable ({fetch_error}), "
            "and no local cache or bundled fallback catalog available."
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s\-_]+", "", text.lower())

    def calculate_score(self, product: ProductSummary, query: str) -> int:
        """Score product relevance against query:
        Tier 1: Exact slug match = 100
        Tier 2: Exact name match = 90
        Tier 3: Prefix match = 75
        Tier 4: Substring match = 60
        Tier 5: Fuzzy difflib match = 45..55
        Non-match = 0
        """
        q_raw = query.strip().lower()
        q_norm = self._normalize(query)
        if not q_norm:
            return 0

        slug_raw = product.slug.lower()
        slug_norm = self._normalize(product.slug)
        name_raw = product.name.lower()
        name_norm = self._normalize(product.name)

        # Tier 1: Exact slug match
        if slug_raw == q_raw or slug_norm == q_norm:
            return 100

        # Tier 2: Exact name match
        if name_raw == q_raw or name_norm == q_norm:
            return 90

        # Tier 3: Prefix match
        if (
            slug_raw.startswith(q_raw)
            or name_raw.startswith(q_raw)
            or slug_norm.startswith(q_norm)
            or name_norm.startswith(q_norm)
        ):
            return 75

        # Tier 4: Substring match
        if (
            q_raw in slug_raw
            or q_raw in name_raw
            or q_norm in slug_norm
            or q_norm in name_norm
        ):
            return 60

        # Tier 5: Fuzzy match via SequenceMatcher
        r_slug = difflib.SequenceMatcher(None, q_norm, slug_norm).ratio()
        r_name = difflib.SequenceMatcher(None, q_norm, name_norm).ratio()
        best_ratio = max(r_slug, r_name)
        if best_ratio >= 0.6:
            return int(30 + 25 * best_ratio)  # 45 to 55

        return 0

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 10,
        refresh: bool = False,
    ) -> List[Tuple[ProductSummary, int]]:
        """Search products across catalog returning sorted list of (ProductSummary, score)."""
        catalog = self.get_catalog(refresh=refresh)
        q_clean = query.strip()

        if not q_clean:
            if category:
                cat_clean = category.strip().lower()
                matches = [
                    (p, 100) for p in catalog
                    if p.category.lower() == cat_clean
                ]
                return matches[:limit]
            return []

        cat_filter = category.strip().lower() if category and category.strip() else None

        scored: List[Tuple[ProductSummary, int]] = []
        for prod in catalog:
            if cat_filter and prod.category.lower() != cat_filter:
                continue
            score = self.calculate_score(prod, q_clean)
            if score > 0:
                scored.append((prod, score))

        scored.sort(key=lambda item: (-item[1], len(item[0].name), item[0].name.lower()))
        return scored[:limit]

    def resolve_product(
        self,
        query_or_slug: str,
        refresh: bool = False,
    ) -> Optional[ProductSummary]:
        """Resolve a query or slug to a specific ProductSummary."""
        q = query_or_slug.strip()
        if not q:
            return None

        catalog = self.get_catalog(refresh=refresh)
        q_lower = q.lower()
        q_norm = self._normalize(q)

        # Pass 1: Exact slug match
        for p in catalog:
            if p.slug.lower() == q_lower:
                return p

        # Pass 2: Exact name match
        for p in catalog:
            if p.name.lower() == q_lower:
                return p

        # Pass 3: Normalized slug or name match
        for p in catalog:
            if self._normalize(p.slug) == q_norm or self._normalize(p.name) == q_norm:
                return p

        # Pass 4: Confident top search match (score >= 50)
        matches = self.search(q, limit=1, refresh=False)
        if matches and matches[0][1] >= 50:
            return matches[0][0]

        return None
