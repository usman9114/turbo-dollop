"""eBay Browse API (official, 5,000 calls/day): "for parts or not working" items on ebay.de.

Get keys at https://developer.ebay.com (Application Keys -> Production). Only the
client-credentials grant is used, so no eBay login is involved.
"""

from __future__ import annotations

import base64
import time

import httpx2 as httpx

from hunter.models import Listing

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CONDITION_FOR_PARTS = "7000"


class EbayClient:
    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_DE"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self._token: str | None = None
        self._expires = 0.0
        self.http = httpx.Client(timeout=30)

    def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.http.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        r.raise_for_status()
        data = r.json()
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 7200))
        return self._token

    def search(self, query: str, *, for_parts: bool = True, max_price: float | None = None,
               postal_code: str | None = None, limit: int = 50) -> list[Listing]:
        filters = ["itemLocationCountry:DE", "buyingOptions:{FIXED_PRICE|AUCTION}"]
        if for_parts:
            filters.append(f"conditionIds:{{{CONDITION_FOR_PARTS}}}")
        if max_price is not None:
            filters.append(f"price:[..{int(max_price)}],priceCurrency:EUR")
        headers = {
            "Authorization": f"Bearer {self._auth()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
        }
        if postal_code:
            # Makes shippingCost reflect delivery to you.
            headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=DE,zip={postal_code}"
        r = self.http.get(SEARCH_URL, headers=headers,
                          params={"q": query, "filter": ",".join(filters), "limit": min(limit, 200),
                                  "sort": "newlyListed"})
        r.raise_for_status()
        return [parse_item(it) for it in r.json().get("itemSummaries", [])]


def parse_item(it: dict) -> Listing:
    price = it.get("price") or it.get("currentBidPrice") or {}
    ship = None
    for opt in it.get("shippingOptions") or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            ship = float(cost) if ship is None else min(ship, float(cost))
    images = [x["imageUrl"] for x in ([it.get("image")] + (it.get("additionalImages") or [])) if x and x.get("imageUrl")]
    loc = it.get("itemLocation") or {}
    buying = it.get("buyingOptions") or []
    return Listing(
        source="ebay",
        ad_id=str(it.get("itemId", "")),
        url=it.get("itemWebUrl", ""),
        title=it.get("title", ""),
        description=it.get("shortDescription", "") or it.get("condition", ""),
        price_eur=float(price["value"]) if price.get("value") is not None else None,
        price_text=f"{price.get('value', '?')} {price.get('currency', '')}".strip()
        + (" (auction)" if "AUCTION" in buying else ""),
        negotiable="BEST_OFFER" in buying,
        location=" ".join(x for x in (loc.get("postalCode"), loc.get("city")) if x),
        postal_code=loc.get("postalCode"),
        shipping_possible=True,
        direct_buy="FIXED_PRICE" in buying,
        ship_in_eur=ship,
        image_urls=images[:6],
        seller_type="commercial" if (it.get("seller") or {}).get("sellerAccountType") == "BUSINESS" else None,
    )
