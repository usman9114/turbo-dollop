"""eBay Browse API (official, 5,000 calls/day on the default tier).

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
"""

from __future__ import annotations

import base64
import logging
import time

import httpx2 as httpx

from ..models import Listing
from ..privacy import seller_hash, strip_pii
from ..query import HuntQuery, ebay_filter

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CATEGORY_LAPTOPS = "177"          # PC Notebooks & Netbooks
CATEGORY_VACUUMS = "20614"        # Vacuum cleaners


class EbayClient:
    def __init__(self, client_id: str, client_secret: str, zip_code: str = "80331", http: httpx.Client | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.zip_code = zip_code
        self.http = http or httpx.Client(timeout=20)
        self._token: str | None = None
        self._token_exp = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.http.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 7200))
        return self._token

    def search(self, q: HuntQuery, parts_only: bool = True, limit: int = 50, category: str | None = None) -> list[Listing]:
        params = {
            "q": q.text,
            "filter": ebay_filter(q, parts_only=parts_only),
            "sort": "newlyListed",
            "limit": str(min(limit, 200)),
        }
        if category:
            params["category_ids"] = category
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE",
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country=DE,zip={self.zip_code}",
            "Accept-Language": "de-DE",
        }
        resp = self.http.get(SEARCH_URL, params=params, headers=headers)
        if resp.status_code == 429:
            log.warning("eBay rate limit reached")
            return []
        resp.raise_for_status()
        return [parse_item(item) for item in resp.json().get("itemSummaries", [])]


def parse_item(item: dict) -> Listing:
    price = item.get("price") or item.get("currentBidPrice") or {}
    ship_opts = item.get("shippingOptions") or []
    ship_cost = None
    if ship_opts:
        cost = (ship_opts[0].get("shippingCost") or {}).get("value")
        ship_cost = float(cost) if cost is not None else None
    loc = item.get("itemLocation") or {}
    images = [(item.get("image") or {}).get("imageUrl")] + [i.get("imageUrl") for i in item.get("additionalImages") or []]
    return Listing(
        source="ebay",
        ad_id=item["itemId"],
        title=item.get("title", ""),
        url=item.get("itemWebUrl", ""),
        price=float(price["value"]) if price.get("value") is not None else None,
        negotiable="BEST_OFFER" in (item.get("buyingOptions") or []),
        description=strip_pii(item.get("shortDescription", "")),
        location=" ".join(filter(None, [loc.get("postalCode"), loc.get("city")])),
        shipping=bool(ship_opts),
        shipping_cost=ship_cost,
        image_urls=[u for u in images if u],
        seller_hash=seller_hash((item.get("seller") or {}).get("username", "")),
        condition=item.get("condition", ""),
        raw_source="api",
    )
