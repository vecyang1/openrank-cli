"""Domain models and data structures for OpenTheRank CLI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ProductSummary:
    """Represents a product in the OpenTheRank search catalog."""

    name: str
    slug: str
    path: str
    category: str
    url: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "path": self.path,
            "category": self.category,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProductSummary:
        return cls(
            name=str(data.get("name", "")),
            slug=str(data.get("slug", "")),
            path=str(data.get("path", "")),
            category=str(data.get("category", "")),
            url=str(data.get("url", "")),
        )


@dataclass
class PriceTier:
    """Represents a specific subscription/edition price tier."""

    name: str
    local_price_str: str
    usd_price: float
    delta_percent: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "local_price_str": self.local_price_str,
            "usd_price": self.usd_price,
            "delta_percent": self.delta_percent,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PriceTier:
        usd_raw = data.get("usd_price")
        usd_price = float(usd_raw) if usd_raw is not None else 0.0
        return cls(
            name=str(data.get("name", "")),
            local_price_str=str(data.get("local_price_str", "")),
            usd_price=usd_price,
            delta_percent=float(data["delta_percent"]) if data.get("delta_percent") is not None else None,
        )


@dataclass
class RegionPricingRecord:
    """Pricing record for a specific country/region and platform."""

    country_code: str
    region_name: str
    platform: str
    primary_tier: PriceTier
    additional_tiers: Dict[str, PriceTier] = field(default_factory=dict)
    vs_us_delta: Optional[float] = None
    tax_info: str = ""
    risk_level: str = "Low"
    risk_note: str = ""
    snapshot_date: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "country_code": self.country_code,
            "region_name": self.region_name,
            "platform": self.platform,
            "primary_tier": self.primary_tier.to_dict(),
            "additional_tiers": {k: v.to_dict() for k, v in self.additional_tiers.items()},
            "vs_us_delta": self.vs_us_delta,
            "tax_info": self.tax_info,
            "risk_level": self.risk_level,
            "risk_note": self.risk_note,
            "snapshot_date": self.snapshot_date,
        }


@dataclass
class ProductPricingResult:
    """Complete multi-region pricing result for a product."""

    product: ProductSummary
    records: List[RegionPricingRecord]
    baseline_us_price: Optional[float] = None
    platforms: List[str] = field(default_factory=list)
    us_baseline_usd: Optional[float] = None
    first_party_validated: bool = False
    validation_source: Optional[str] = None
    validation_notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.baseline_us_price is not None and self.us_baseline_usd is None:
            self.us_baseline_usd = self.baseline_us_price
        elif self.us_baseline_usd is not None and self.baseline_us_price is None:
            self.baseline_us_price = self.us_baseline_usd
        if not self.platforms and self.records:
            seen: List[str] = []
            for r in self.records:
                if r.platform and r.platform not in seen:
                    seen.append(r.platform)
            self.platforms = seen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product": self.product.to_dict(),
            "records": [r.to_dict() for r in self.records],
            "baseline_us_price": self.baseline_us_price,
            "platforms": self.platforms,
            "us_baseline_usd": self.us_baseline_usd,
            "first_party_validated": self.first_party_validated,
            "validation_source": self.validation_source,
            "validation_notes": list(self.validation_notes),
        }


@dataclass
class AppleIAPItem:
    """In-App Purchase item parsed from Apple App Store SSR hydration data."""

    name: str
    local_price_str: str
    numeric_price: Optional[float] = None
    currency_symbol: Optional[str] = None
    raw_amount: Optional[float] = None
    tier_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.tier_name and not self.name:
            self.name = self.tier_name
        elif self.name and not self.tier_name:
            self.tier_name = self.name
        if self.raw_amount is not None and self.numeric_price is None:
            self.numeric_price = self.raw_amount
        elif self.numeric_price is not None and self.raw_amount is None:
            self.raw_amount = self.numeric_price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tier_name": self.tier_name or self.name,
            "local_price_str": self.local_price_str,
            "numeric_price": self.numeric_price,
            "currency_symbol": self.currency_symbol,
            "raw_amount": self.raw_amount,
        }


@dataclass
class AppleValidationResult:
    """Result of first-party Apple App Store validation and anti-spoofing check."""

    product_slug: str
    country_code: str
    requested_url: str = ""
    final_url: str = ""
    is_spoofed: bool = False
    items: List[AppleIAPItem] = field(default_factory=list)
    error: Optional[str] = None
    is_valid: bool = True
    status_code: int = 200
    app_id: str = ""
    redirect_target: Optional[str] = None
    iaps: List[AppleIAPItem] = field(default_factory=list)
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if self.items and not self.iaps:
            self.iaps = list(self.items)
        elif self.iaps and not self.items:
            self.items = list(self.iaps)
        if self.error and not self.error_message:
            self.error_message = self.error
        elif self.error_message and not self.error:
            self.error = self.error_message
        if self.final_url and not self.redirect_target:
            self.redirect_target = self.final_url
        elif self.redirect_target and not self.final_url:
            self.final_url = self.redirect_target
        if self.error or self.is_spoofed or self.status_code >= 400:
            self.is_valid = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_slug": self.product_slug,
            "country_code": self.country_code,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "is_spoofed": self.is_spoofed,
            "is_valid": self.is_valid,
            "status_code": self.status_code,
            "app_id": self.app_id,
            "redirect_target": self.redirect_target,
            "items": [item.to_dict() for item in self.items],
            "error": self.error,
            "error_message": self.error_message,
        }
