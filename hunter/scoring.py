"""Expected-value scoring (plan §4.2, with §25a VAT and the warranty reserve).

    net_sale = resale - sale_fee - ship_out - packaging - VAT_25a - warranty_reserve
    cost_in  = buy + buy_fee + parts + travel (or inbound shipping)
    EV       = p_ok * net_sale + (1 - p_ok) * salvage - cost_in
    alert if EV >= min_ev AND EV/hour >= min_ev_per_hour AND EV/(buy+parts) >= min_roi (or free)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hunter.assessment import CHARGER_COST_EUR, EV_MULTIPLIERS, REJECT_FLAGS, Assessment
from hunter.config import Settings
from hunter.models import Listing
from hunter.prices import ModelPrice, PriceTable


@dataclass
class Score:
    ev: float | None = None
    ev_per_hour: float | None = None
    roi: float | None = None
    hours: float | None = None
    buy_assumed: float | None = None
    max_buy: float | None = None  # highest price that still clears min_ev
    pickup: bool = True
    alert: bool = False
    rejected: str | None = None
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def rank_value(self) -> float:
        if self.rejected:
            return -1e9
        if self.ev is None:
            # unknown model / price: rank by headroom if we have it, else at the bottom
            return (self.max_buy or 0.0) - 1e6
        return self.ev


def assumed_buy_price(listing: Listing, s: Settings) -> float | None:
    if listing.free:
        return 0.0
    if listing.price_eur is None:
        return None
    return round(listing.price_eur * (s.vb_discount if listing.negotiable else 1.0), 2)


def _logistics(listing: Listing, s: Settings) -> tuple[bool, float, float]:
    """(pickup?, € travel or inbound shipping, hours of travel)."""
    dist = listing.distance_km
    if dist is None and listing.source == "kleinanzeigen" and not listing.shipping_possible:
        dist = s.radius_km / 2  # unknown, assume mid-radius
    if dist is not None and (dist <= s.radius_km or not listing.shipping_possible):
        return True, 2 * dist * s.km_cost_eur, 2 * dist / s.drive_kmh
    ship = listing.ship_in_eur if listing.ship_in_eur is not None else s.ship_in_eur
    return False, ship, 0.0


def _ev_for(buy: float, model: ModelPrice, a: Assessment, listing: Listing, s: Settings) -> tuple[float, dict]:
    resale = model.resale_p50
    pickup, logistics, _ = _logistics(listing, s)
    sale_fee = resale * s.sale_fee_pct
    ship_out = model.ship_out if model.ship_out is not None else s.ship_out_eur
    vat = max(0.0, resale - buy) * 19 / 119 if s.vat_mode == "25a" else 0.0
    warranty = resale * s.warranty_pct
    net_sale = resale - sale_fee - ship_out - s.packaging_eur - vat - warranty

    # Buyer-protection fee applies when paying through the platform (shipped / "Direkt kaufen").
    buy_fee = 0.0 if (pickup or buy == 0) else buy * s.buy_fee_pct + s.buy_fee_fixed
    parts = a.parts_cost_eur + (CHARGER_COST_EUR if "no_charger" in a.red_flags else 0.0)
    cost_in = buy + buy_fee + parts + logistics

    ev = a.p_ok * net_sale + (1 - a.p_ok) * model.salvage - cost_in
    breakdown = {
        "resale": resale,
        "sale_fee": round(sale_fee, 2),
        "ship_out": ship_out + s.packaging_eur,
        "vat_25a": round(vat, 2),
        "warranty": round(warranty, 2),
        "net_sale": round(net_sale, 2),
        "buy": buy,
        "buy_fee": round(buy_fee, 2),
        "parts": parts,
        "travel_or_ship_in": round(logistics, 2),
        "cost_in": round(cost_in, 2),
        "p_ok": a.p_ok,
        "salvage": model.salvage,
    }
    return ev, breakdown


def _apply_multiplier(ev: float, a: Assessment) -> tuple[float, list[str]]:
    reasons = []
    mult = 1.0
    for flag, m in EV_MULTIPLIERS.items():
        if flag in a.red_flags:
            mult *= m
            reasons.append(f"{flag} ×{m}")
    return (ev * mult if ev > 0 else ev), reasons


def score(listing: Listing, a: Assessment, prices: PriceTable, s: Settings) -> Score:
    sc = Score()
    hard = sorted(set(a.red_flags) & REJECT_FLAGS)
    if hard:
        sc.rejected = ", ".join(hard)
    if listing.status != "active":
        sc.rejected = listing.status
    if not a.relevant and not sc.rejected:
        sc.rejected = "not relevant to the query"

    model = prices.get(a.model_key) or prices.match(f"{listing.title}\n{listing.description}")
    pickup, _, travel_h = _logistics(listing, s)
    sc.pickup = pickup
    sc.hours = round(a.repair_hours + s.overhead_hours + travel_h, 2)

    if model is None:
        sc.reasons.append("model not in prices.yaml - add it to get an EV")
        return sc

    # Highest buy price that still clears min_ev (EV falls as buy rises; bisection).
    lo, hi = 0.0, model.resale_p50
    ev_lo, _ = _ev_for(lo, model, a, listing, s)
    if _apply_multiplier(ev_lo, a)[0] >= s.min_ev:
        for _ in range(40):
            mid = (lo + hi) / 2
            if _apply_multiplier(_ev_for(mid, model, a, listing, s)[0], a)[0] >= s.min_ev:
                lo = mid
            else:
                hi = mid
        sc.max_buy = round(lo, 0)
    else:
        sc.max_buy = 0.0

    buy = assumed_buy_price(listing, s)
    sc.buy_assumed = buy
    if buy is None:
        sc.reasons.append("no price given - ask; pay at most the max buy price")
        return sc

    ev, breakdown = _ev_for(buy, model, a, listing, s)
    ev, mult_reasons = _apply_multiplier(ev, a)
    sc.reasons += mult_reasons
    sc.ev = round(ev, 2)
    sc.breakdown = breakdown
    sc.ev_per_hour = round(ev / sc.hours, 2) if sc.hours else None
    invested = buy + breakdown["parts"]
    sc.roi = round(ev / invested, 3) if invested > 0 else None

    roi_ok = buy == 0 or (sc.roi is not None and sc.roi >= s.min_roi)
    sc.alert = (
        sc.rejected is None
        and sc.ev >= s.min_ev
        and (sc.ev_per_hour or 0) >= s.min_ev_per_hour
        and roi_ok
    )
    if sc.ev < s.min_ev:
        sc.reasons.append(f"EV below €{s.min_ev:.0f}")
    return sc
