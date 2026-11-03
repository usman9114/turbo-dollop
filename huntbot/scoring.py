"""Expected-value scoring (plan section 4.2, C + D merged), with two modes.

repair : broken device → fix → sell.        hours/parts from the screen.
flip   : working device bought under market → clean, wipe, photos → sell.  ~45 min, ~€3.

net_sale = resale − platform_fee − ship_out − packaging − VAT_25a − warranty_reserve
cost_in  = buy + buy_fee + parts + travel (or inbound shipping)
EV       = p_ok × net_sale + (1 − p_ok) × net_salvage − cost_in

Resale comes from (in order): a catalog row labelled SOLD, the price model (both platforms,
best net wins), or a catalog EST row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import catalog, features
from .config import Economics
from .models import Listing, Score, Screen
from .pricemodel import PriceModel, can_price

REJECT_FLAGS = {"activation_lock", "mdm_or_autopilot", "not_the_device"}
EV_MULTIPLIERS = {"bios_password": 0.5, "stock_photos": 0.7, "company_asset_tag": 0.5}
CHARGER_COST = 25.0


def vat_25a(sale: float, buy: float, econ: Economics) -> float:
    """§25a margin scheme: VAT is contained in the positive margin."""
    if econ.kleinunternehmer:
        return 0.0
    margin = sale - buy
    return margin * econ.vat_rate / (1 + econ.vat_rate) if margin > 0 else 0.0


def net_of_sale(sale: float, buy: float, econ: Economics, platform: str = "ebay") -> float:
    """eBay: 5% fee + shipping out. Kleinanzeigen: local pickup, no fee, no shipping."""
    if platform == "ebay":
        fees, ship = sale * econ.sell_fee_rate, econ.ship_out + econ.packaging
    else:
        fees, ship = 0.0, 0.0
    return sale - fees - ship - vat_25a(sale, buy, econ) - sale * econ.warranty_reserve


def is_far(listing: Listing, econ: Economics) -> bool:
    m = re.match(r"\s*(\d{5})", listing.location or "")
    return bool(m) and not m.group(1).startswith(econ.home_plz_prefixes)


def buy_estimate(listing: Listing, econ: Economics) -> float:
    if listing.price is None:
        return 0.0
    return listing.price * (econ.vb_discount if listing.negotiable else 1.0)


@dataclass
class Valuation:
    mode: str                         # flip | repair
    resale_by_platform: dict[str, float]
    salvage: float
    hours: float
    parts: float
    p_ok: float
    fair_ask: float | None = None     # typical asking price for THIS listing (platform, condition)
    low: float | None = None
    high: float | None = None
    source: str = ""
    label: str = "EST"


def valuation(listing: Listing, screen: Screen, econ: Economics, model: PriceModel | None) -> Valuation | None:
    entry = catalog.by_key(screen.catalog_key)
    feats = features.extract(listing.title, listing.description, listing.source,
                             negotiable=listing.negotiable, condition_field=listing.condition)
    working = screen.fault_category == "none" and feats.condition != "defect"
    mode = "flip" if working else "repair"
    # After cleaning (flip) or repair the device sells as "good", keeping like-new/new/worn as is.
    sell_cond = feats.condition if feats.condition in ("new", "like_new", "worn") else "good"

    if mode == "flip":
        hours, parts, p_ok = econ.flip_hours, econ.flip_parts, min(econ.flip_p_ok, econ.p_ok_cap)
    else:
        hours, parts, p_ok = max(0.25, screen.repair_hours), max(0.0, screen.parts_cost_eur), screen.p_ok

    priced = model is not None and can_price(feats)
    fair_ask = low = high = None
    if priced:
        fair_ask = model.predict(feats).price                          # what this one typically asks
    if entry is not None and entry.label == "SOLD":
        resale = {"ebay": entry.resale_p50}
        salvage, src, label = entry.salvage, f"catalog {entry.key} (SOLD)", "SOLD"
    elif priced:
        out = {
            platform: model.predict(replace(feats, platform=platform, price_kind="sold", condition=sell_cond,
                                            negotiable=False, charger=True if feats.charger is None else feats.charger))
            for platform in ("ebay", "kleinanzeigen")
        }
        resale = {p: e.price for p, e in out.items()}
        low, high = out["ebay"].low, out["ebay"].high
        defect = model.predict(replace(feats, platform="ebay", price_kind="sold", condition="defect", negotiable=False))
        salvage = entry.salvage if entry else 0.8 * defect.price
        src, label = out["ebay"].source, "MODEL"
    elif entry is not None:
        resale = {"ebay": entry.resale_p50}
        salvage, src, label = entry.salvage, f"catalog {entry.key} (EST)", entry.label
    else:
        return None
    return Valuation(mode=mode, resale_by_platform=resale, salvage=salvage, hours=hours, parts=parts, p_ok=p_ok,
                     fair_ask=fair_ask, low=low, high=high, source=src, label=label)


def _ev(buy: float, listing: Listing, screen: Screen, v: Valuation, econ: Economics) -> tuple[float, float, float, str, float]:
    """Returns (ev, net_sale, cost_in, sell_platform, resale) for the better sell platform."""
    p_ok = max(0.0, min(v.p_ok, econ.p_ok_cap))
    parts = v.parts + (CHARGER_COST if "no_charger" in screen.red_flags else 0.0)

    if listing.source == "kleinanzeigen":
        shipped = bool(listing.shipping) or is_far(listing, econ)   # far away: only worth it shipped
        buy_fee = (buy * econ.buy_fee_rate + econ.buy_fee_fixed) if shipped else 0.0
        inbound = econ.ship_in if shipped else econ.default_travel_km * econ.km_cost
    else:  # eBay: shipping cost from the API, no buyer fee modelled
        buy_fee = 0.0
        inbound = listing.shipping_cost if listing.shipping_cost is not None else econ.ship_in
    cost_in = buy + buy_fee + parts + inbound
    net_salvage = net_of_sale(v.salvage, buy, econ, "ebay") if v.salvage > 0 else 0.0

    best = None
    for platform, resale in v.resale_by_platform.items():
        net = net_of_sale(resale, buy, econ, platform)
        ev = p_ok * net + (1 - p_ok) * net_salvage - cost_in
        if best is None or ev > best[0]:
            best = (ev, net, cost_in, platform, resale)
    return best


def _apply_flags(ev: float, flags: list[str]) -> tuple[float, list[str]]:
    reasons = []
    for flag in flags:
        mult = EV_MULTIPLIERS.get(flag)
        if mult and ev > 0:
            ev *= mult
            reasons.append(f"{flag} ×{mult}")
    return ev, reasons


def max_buy_price(listing: Listing, screen: Screen, econ: Economics, target_ev: float | None = None,
                  model: PriceModel | None = None, v: Valuation | None = None) -> float:
    """Highest buy price that still meets the EV threshold (bisection; EV falls as buy rises)."""
    v = v or valuation(listing, screen, econ, model)
    if v is None:
        return 0.0
    if target_ev is None:
        target_ev = econ.flip_min_ev if v.mode == "flip" else econ.min_ev

    def ok(buy: float) -> bool:
        ev, *_ = _ev(buy, listing, screen, v, econ)
        ev, _ = _apply_flags(ev, screen.red_flags)
        return ev >= target_ev

    lo, hi = 0.0, max(v.resale_by_platform.values())
    if not ok(lo):
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    return float(int(lo))


def score(listing: Listing, screen: Screen, econ: Economics, model: PriceModel | None = None) -> Score:
    buy = buy_estimate(listing, econ)
    hard_flags = sorted(REJECT_FLAGS & set(screen.red_flags))
    v = valuation(listing, screen, econ, model)
    if v is None:
        return Score(ev=0.0, ev_per_hour=0.0, roi=0.0, buy_estimate=buy, max_buy=0.0, resale=0.0,
                     net_sale=0.0, cost_in=buy, hours=max(0.25, screen.repair_hours), p_ok=screen.p_ok, alert=False,
                     rejected=bool(hard_flags),
                     reasons=(["REJECT: " + ", ".join(hard_flags)] if hard_flags else [])
                     + ["unknown model: add it to huntbot/data/catalog.csv"])

    reasons: list[str] = []
    ev, net_sale, cost_in, sell_on, resale = _ev(buy, listing, screen, v, econ)
    ev, flag_reasons = _apply_flags(ev, screen.red_flags)
    reasons += flag_reasons
    reasons.append(f"sell on {sell_on} ~€{resale:.0f} ({v.source})")

    rejected = bool(hard_flags)
    if rejected:
        reasons.insert(0, "REJECT: " + ", ".join(hard_flags))

    invested = buy + v.parts
    roi = ev / invested if invested > 0 else (99.0 if ev > 0 else 0.0)
    ev_per_hour = ev / v.hours
    discount = (1 - buy / v.fair_ask) if (v.fair_ask and listing.price) else None
    free = listing.price == 0

    if v.mode == "flip":
        good = (ev >= econ.flip_min_ev and ev_per_hour >= econ.min_ev_per_hour
                and (discount is None or discount >= econ.flip_min_discount))
        if discount is not None:
            reasons.append(f"{discount:.0%} under typical {listing.source} price €{v.fair_ask:.0f}")
    else:
        good = ev >= econ.min_ev and ev_per_hour >= econ.min_ev_per_hour and roi >= econ.min_roi
    alert = (not rejected) and listing.price is not None and ((free and ev > 0) or good)

    if listing.price is None:
        reasons.append("no price found")
    if listing.source == "kleinanzeigen" and is_far(listing, econ):
        if listing.shipping:
            reasons.append("outside pickup area: priced with shipping")
        else:
            alert = False
            reasons.append("outside pickup area and no shipping offered: ask seller to ship")

    return Score(
        ev=ev, ev_per_hour=ev_per_hour, roi=roi, buy_estimate=buy,
        max_buy=0.0 if rejected else max_buy_price(listing, screen, econ, model=model, v=v),
        resale=resale, net_sale=net_sale, cost_in=cost_in, hours=v.hours, p_ok=min(v.p_ok, econ.p_ok_cap),
        alert=alert, rejected=rejected, reasons=reasons, mode=v.mode, sell_on=sell_on,
        fair_ask=v.fair_ask, discount=discount, price_low=v.low, price_high=v.high, price_source=v.source,
    )
