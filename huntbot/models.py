"""Core data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

Source = Literal["kleinanzeigen", "ebay"]

FaultCategory = Literal[
    "none", "software", "storage", "battery", "keyboard", "screen",
    "hinge_case", "port_charging", "mainboard", "liquid", "missing_parts", "unknown",
]

RedFlag = Literal[
    "activation_lock", "mdm_or_autopilot", "bios_password", "stock_photos",
    "company_asset_tag", "no_charger", "liquid_damage", "suspicious_urgency",
    "parts_missing", "not_the_device",
    "dealer_listing", "scam_pattern", "wanted_ad",
]


@dataclass
class Listing:
    source: Source
    ad_id: str
    title: str
    url: str
    price: float | None = None          # asking price in EUR; None = unknown, 0 = free
    negotiable: bool = False            # "VB"
    description: str = ""
    location: str = ""                  # postcode / city only (GDPR: no street, no names)
    shipping: bool | None = None        # seller offers shipping
    shipping_cost: float | None = None
    image_urls: list[str] = field(default_factory=list)
    seller_hash: str = ""
    condition: str = ""                 # e.g. eBay "For parts or not working"
    raw_source: str = ""                # "email", "companion", "api"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ad_id}"


class Screen(BaseModel):
    """What the model (or the rule engine) believes about a listing."""

    model_config = ConfigDict(extra="forbid")

    device_model: str
    catalog_key: str
    fault_summary: str
    fault_category: FaultCategory
    p_ok: float
    parts_cost_eur: float
    repair_hours: float
    red_flags: list[RedFlag]
    confidence: Literal["low", "medium", "high"]
    questions_for_seller: list[str]


@dataclass
class Score:
    ev: float
    ev_per_hour: float
    roi: float
    buy_estimate: float
    max_buy: float
    resale: float
    net_sale: float
    cost_in: float
    hours: float
    p_ok: float
    alert: bool
    rejected: bool
    reasons: list[str] = field(default_factory=list)
    mode: str = "repair"               # repair | flip
    sell_on: str = "ebay"              # platform with the better net
    fair_ask: float | None = None      # typical asking price for this listing on its platform
    discount: float | None = None      # 1 − buy / fair_ask
    price_low: float | None = None     # 80% interval of the eBay resale estimate
    price_high: float | None = None
    price_source: str = ""

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}
