"""Plain data types shared by every source, the screener and the scorer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Listing:
    source: str  # "kleinanzeigen" | "ebay"
    ad_id: str
    url: str
    title: str
    description: str = ""
    price_eur: float | None = None
    price_text: str = ""
    negotiable: bool = False
    free: bool = False
    location: str = ""
    postal_code: str | None = None
    distance_km: float | None = None
    posted: str = ""
    shipping_possible: bool = False
    direct_buy: bool = False
    ship_in_eur: float | None = None  # known shipping cost to you (eBay)
    image_urls: list[str] = field(default_factory=list)
    photo_count: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    seller_type: str | None = None  # "private" | "commercial" | None (unknown)
    status: str = "active"  # "active" | "reserved" | "sold" | "deleted"
    detailed: bool = False  # True once the full ad page was read

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ad_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Listing":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
