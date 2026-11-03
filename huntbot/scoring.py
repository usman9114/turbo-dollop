"""Expected-value scoring (plan section 4.2, C + D merged).

net_sale = resale − platform_fee − ship_out − packaging − VAT_25a − warranty_reserve
cost_in  = buy + buy_fee + parts + travel (or inbound shipping)
EV       = p_ok × net_sale + (1 − p_ok) × net_salvage − cost_in
"""

from __future__ import annotations

from . import catalog
from .config import Economics
from .models import Listing, Score, Screen

REJECT_FLAGS = {"activation_lock", "mdm_or_autopilot", "not_the_device"}
EV_MULTIPLIERS = {"bios_password": 0.5, "stock_photos": 0.7, "company_asset_tag": 0.5}
CHARGER_COST = 25.0


def vat_25a(sale: float, buy: float, econ: Economics) -> float:
    """§25a margin scheme: VAT is contained in the positive margin."""
    if econ.kleinunternehmer:
        return 0.0
    margin = sale - buy
    return margin * econ.vat_rate / (1 + econ.vat_rate) if margin > 0 else 0.0


def _net_of_sale(sale: float, buy: float, econ: Economics, shipped: bool = True) -> float:
    fees = sale * econ.sell_fee_rate
    ship = (econ.ship_out + econ.packaging) if shipped else 0.0
    return sale - fees - ship - vat_25a(sale, buy, econ) - sale * econ.warranty_reserve


def buy_estimate(listing: Listing, econ: Economics) -> float:
    if listing.price is None:
        return 0.0
    return listing.price * (econ.vb_discount if listing.negotiable else 1.0)


def _ev(buy: float, listing: Listing, screen: Screen, resale: float, salvage: float, econ: Economics) -> tuple[float, float, float, float]:
    p_ok = max(0.0, min(screen.p_ok, econ.p_ok_cap))
    parts = max(0.0, screen.parts_cost_eur)
    if "no_charger" in screen.red_flags:
        parts += CHARGER_COST

    if listing.source == "kleinanzeigen":
        buy_fee = (buy * econ.buy_fee_rate + econ.buy_fee_fixed) if listing.shipping else 0.0
        inbound = econ.ship_in if listing.shipping else econ.default_travel_km * econ.km_cost
    else:  # eBay: shipping cost from the API, no buyer fee modelled
        buy_fee = 0.0
        inbound = listing.shipping_cost if listing.shipping_cost is not None else econ.ship_in

    net_sale = _net_of_sale(resale, buy, econ)
    net_salvage = _net_of_sale(salvage, buy, econ, shipped=False) if salvage > 0 else 0.0
    cost_in = buy + buy_fee + parts + inbound
    ev = p_ok * net_sale + (1 - p_ok) * net_salvage - cost_in
    return ev, net_sale, cost_in, parts


def _apply_flags(ev: float, flags: list[str]) -> tuple[float, list[str]]:
    reasons = []
    for flag in flags:
        mult = EV_MULTIPLIERS.get(flag)
        if mult and ev > 0:
            ev *= mult
            reasons.append(f"{flag} ×{mult}")
    return ev, reasons


def max_buy_price(listing: Listing, screen: Screen, econ: Economics, target_ev: float | None = None) -> float:
    """Highest buy price that still meets the EV threshold (bisection; EV falls as buy rises)."""
    entry = catalog.by_key(screen.catalog_key)
    if entry is None:
        return 0.0
    target = econ.min_ev if target_ev is None else target_ev
    lo, hi = 0.0, entry.resale_p50
    def ok(buy: float) -> bool:
        ev, *_ = _ev(buy, listing, screen, entry.resale_p50, entry.salvage, econ)
        ev, _ = _apply_flags(ev, screen.red_flags)
        return ev >= target
    if not ok(lo):
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    return float(int(lo))  # round down to whole euros


def score(listing: Listing, screen: Screen, econ: Economics) -> Score:
    entry = catalog.by_key(screen.catalog_key)
    buy = buy_estimate(listing, econ)
    hours = max(0.25, screen.repair_hours)
    reasons: list[str] = []

    if entry is None:
        return Score(ev=0.0, ev_per_hour=0.0, roi=0.0, buy_estimate=buy, max_buy=0.0, resale=0.0,
                     net_sale=0.0, cost_in=buy, hours=hours, p_ok=screen.p_ok, alert=False,
                     rejected=False, reasons=["unknown model: add it to huntbot/data/catalog.csv"])

    ev, net_sale, cost_in, parts = _ev(buy, listing, screen, entry.resale_p50, entry.salvage, econ)
    ev, flag_reasons = _apply_flags(ev, screen.red_flags)
    reasons += flag_reasons
    if entry.label != "SOLD":
        reasons.append(f"resale €{entry.resale_p50:.0f} is {entry.label}")

    rejected = bool(REJECT_FLAGS & set(screen.red_flags))
    if rejected:
        reasons.insert(0, "REJECT: " + ", ".join(sorted(REJECT_FLAGS & set(screen.red_flags))))

    invested = buy + parts
    roi = ev / invested if invested > 0 else (float("inf") if ev > 0 else 0.0)
    ev_per_hour = ev / hours
    free = listing.price == 0
    alert = (not rejected) and listing.price is not None and (
        (free and ev > 0)
        or (ev >= econ.min_ev and ev_per_hour >= econ.min_ev_per_hour and roi >= econ.min_roi)
    )
    if listing.price is None:
        reasons.append("no price found")

    return Score(
        ev=ev, ev_per_hour=ev_per_hour, roi=roi if roi != float("inf") else 99.0, buy_estimate=buy,
        max_buy=0.0 if rejected else max_buy_price(listing, screen, econ), resale=entry.resale_p50, net_sale=net_sale,
        cost_in=cost_in, hours=hours, p_ok=min(screen.p_ok, econ.p_ok_cap), alert=alert,
        rejected=rejected, reasons=reasons,
    )
