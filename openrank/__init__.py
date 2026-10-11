"""OpenTheRank CLI — Global pricing, subscription rankings, and currency arbitrage spreads."""

__version__ = "0.1.0"

from openrank.cache import CacheManager
from openrank.catalog import CatalogError, CatalogManager
from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
    fetch_bytes,
    fetch_with_redirect_check,
)
from openrank.models import (
    AppleIAPItem,
    AppleValidationResult,
    PriceTier,
    ProductPricingResult,
    ProductSummary,
    RegionPricingRecord,
)

__all__ = [
    "__version__",
    "ProductSummary",
    "PriceTier",
    "RegionPricingRecord",
    "ProductPricingResult",
    "AppleIAPItem",
    "AppleValidationResult",
    "HttpClient",
    "HttpError",
    "HttpFastFailError",
    "NetworkError",
    "fetch_bytes",
    "fetch_with_redirect_check",
    "CacheManager",
    "CatalogManager",
    "CatalogError",
]
