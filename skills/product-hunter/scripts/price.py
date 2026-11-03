#!/usr/bin/env python3
"""Product-hunter pricing tool (stdlib only).

  price.py laptop "Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB" [--desc "..."] [--ask 180] [--platform kleinanzeigen]
  price.py generic --new-price 999 --age 2 --category phone --condition good [--ask 450] [--platform ebay]
  price.py flip --buy 150 --sell 300 --sell-on ebay [--hours 0.75] [--shipped-in] [--kleinunternehmer]

Laptops / Dyson use the huntbot regression model (log-price on line, CPU generation, RAM, SSD,
condition, platform). If model.json sits next to this script (exported with `huntbot model --export`)
it carries the user's trained coefficients; otherwise the priors are used.
Everything else uses category depreciation curves. All outputs are estimates and say so.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:                                   # bundled copy (dist/product-hunter.zip)
    from huntbot_core.features import extract
    from huntbot_core.pricemodel import PriceModel, can_price
except ImportError:                    # running from the repo
    sys.path.insert(0, str(HERE.parents[2]))
    from huntbot.features import extract
    from huntbot.pricemodel import PriceModel, can_price

# ---- economics (same constants as huntbot/config.py, plan §4.2) -------------
VAT, WARRANTY, EBAY_FEE, SHIP_OUT, PACKAGING = 0.19, 0.04, 0.05, 7.69, 1.50
SHIP_IN, BUY_FEE_RATE, BUY_FEE_FIXED, TRAVEL = 6.99, 0.045, 0.50, 6.00

# ---- generic depreciation priors (rough rules of thumb, Germany 2026) -------
#   value(good, used, eBay sold) ≈ new_price × (1 − first_year_drop) × exp(−k × (age − 1))
CURVES = {
    "phone": (0.30, 0.18), "laptop": (0.25, 0.15), "tablet": (0.25, 0.15), "smartwatch": (0.35, 0.20),
    "camera": (0.20, 0.08), "lens": (0.15, 0.05), "console": (0.15, 0.10), "headphones": (0.30, 0.15),
    "appliance": (0.30, 0.12), "vacuum": (0.30, 0.12), "coffee_machine": (0.30, 0.10), "tool": (0.25, 0.07),
    "bike": (0.25, 0.10), "ebike": (0.30, 0.12), "furniture": (0.45, 0.08), "watch_luxury": (0.10, 0.01),
    "other": (0.30, 0.12),
}
CONDITION = {"new": 1.25, "like_new": 1.10, "good": 1.00, "worn": 0.85, "defect": 0.35}
PLATFORM = {("ebay", "sold"): 1.00, ("ebay", "ask"): 1.10, ("kleinanzeigen", "ask"): 1.05, ("kleinanzeigen", "sold"): 0.93}


def load_model() -> PriceModel:
    path = HERE / "model.json"
    return PriceModel.from_json(path.read_text()) if path.exists() else PriceModel()


def net_of_sale(sale: float, buy: float, sell_on: str, kleinunternehmer: bool) -> float:
    fees, ship = (sale * EBAY_FEE, SHIP_OUT + PACKAGING) if sell_on == "ebay" else (0.0, 0.0)
    vat = 0.0 if kleinunternehmer else max(0.0, sale - buy) * VAT / (1 + VAT)
    return sale - fees - ship - vat - sale * WARRANTY


def flip_ev(buy: float, sell: float, sell_on: str, hours: float = 0.75, shipped_in: bool = False,
            p_ok: float = 0.9, salvage: float = 0.0, kleinunternehmer: bool = False, parts: float = 3.0) -> dict:
    cost_in = buy + parts + ((buy * BUY_FEE_RATE + BUY_FEE_FIXED + SHIP_IN) if shipped_in else TRAVEL)
    net = net_of_sale(sell, buy, sell_on, kleinunternehmer)
    net_salvage = net_of_sale(salvage, buy, "ebay", kleinunternehmer) if salvage else 0.0
    ev = p_ok * net + (1 - p_ok) * net_salvage - cost_in
    return {"ev": round(ev, 2), "ev_per_hour": round(ev / hours, 2), "net_sale": round(net, 2),
            "cost_in": round(cost_in, 2), "roi": round(ev / max(buy + parts, 1), 3),
            "verdict": "BUY" if ev >= 30 and ev / hours >= 25 else ("MAYBE" if ev >= 15 else "PASS")}


def max_buy(sell: float, sell_on: str, target_ev: float = 30.0, **kw) -> float:
    lo, hi = 0.0, sell
    if flip_ev(0.0, sell, sell_on, **kw)["ev"] < target_ev:
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if flip_ev(mid, sell, sell_on, **kw)["ev"] >= target_ev else (lo, mid)
    return float(int(lo))


def cmd_laptop(a) -> dict:
    f = extract(a.title, a.desc, a.platform, negotiable=a.vb)
    if not can_price(f):
        return {"error": "No known product line / CPU generation found. Use `generic` with the new price instead.",
                "features": f.as_dict()}
    m = load_model()
    sell_cond = f.condition if f.condition in ("new", "like_new", "worn") else "good"
    est = {}
    for platform in ("kleinanzeigen", "ebay"):
        for kind in ("ask", "sold"):
            cond = f.condition if kind == "ask" else sell_cond
            e = m.predict(replace(f, platform=platform, price_kind=kind, condition=cond, negotiable=False))
            est[f"{platform}_{kind}"] = e.as_dict()
    out = {"features": f.as_dict(), "estimates_eur": est, "model": m.predict(f).source,
           "note": "80% ranges. Asking prices on Kleinanzeigen run above what things actually sell for."}
    if a.ask is not None:
        fair = m.predict(f).price
        buy = a.ask * (0.85 if a.vb else 1.0)
        options = {}
        for sell_on in ("ebay", "kleinanzeigen"):
            sell = est[f"{sell_on}_sold"]["price"]
            options[sell_on] = {**flip_ev(buy, sell, sell_on, shipped_in=a.shipped_in,
                                          hours=0.75 if f.condition != "defect" else 2.0),
                                "sell_price": sell,
                                "max_buy": max_buy(sell, sell_on, shipped_in=a.shipped_in)}
        best = max(options, key=lambda k: options[k]["ev"])
        out["deal"] = {"asking": a.ask, "expected_buy": round(buy, 2), "typical_ask_same_platform": round(fair),
                       "discount_vs_typical": round(1 - buy / fair, 3), "best_exit": best, "exits": options}
    return out


def cmd_generic(a) -> dict:
    first_drop, k = CURVES.get(a.category, CURVES["other"])
    base = a.new_price * (1 - first_drop) * math.exp(-k * max(0.0, a.age - 1)) if a.age > 0 else a.new_price * 0.92
    base *= CONDITION[a.condition]
    prices = {f"{p}_{kind}": round(base * mult) for (p, kind), mult in PLATFORM.items()}
    out = {"category": a.category, "curve": {"first_year_drop": first_drop, "yearly_decay": k},
           "estimates_eur": prices, "range_note": "rule-of-thumb curve, typically ±25%; verify with sold listings"}
    if a.ask is not None:
        typical = prices[f"{a.platform}_ask"]
        sell_on = "ebay" if prices["ebay_sold"] * 0.95 - 9 > prices["kleinanzeigen_sold"] else "kleinanzeigen"
        out["deal"] = {"asking": a.ask, "typical_ask_same_platform": typical,
                       "discount_vs_typical": round(1 - a.ask / typical, 3),
                       "flip": {**flip_ev(a.ask, prices[f"{sell_on}_sold"], sell_on), "sell_on": sell_on}}
    return out


def cmd_flip(a) -> dict:
    return {**flip_ev(a.buy, a.sell, a.sell_on, a.hours, a.shipped_in, kleinunternehmer=a.kleinunternehmer),
            "max_buy_for_30_ev": max_buy(a.sell, a.sell_on, hours=a.hours, shipped_in=a.shipped_in,
                                         kleinunternehmer=a.kleinunternehmer)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("laptop")
    lp.add_argument("title")
    lp.add_argument("--desc", default="")
    lp.add_argument("--ask", type=float)
    lp.add_argument("--vb", action="store_true", help="price is negotiable (VB)")
    lp.add_argument("--platform", choices=["kleinanzeigen", "ebay"], default="kleinanzeigen")
    lp.add_argument("--shipped-in", action="store_true", help="seller ships (adds fee + shipping)")
    gp = sub.add_parser("generic")
    gp.add_argument("--new-price", type=float, required=True)
    gp.add_argument("--age", type=float, required=True, help="years since bought / released")
    gp.add_argument("--category", choices=sorted(CURVES), default="other")
    gp.add_argument("--condition", choices=sorted(CONDITION), default="good")
    gp.add_argument("--ask", type=float)
    gp.add_argument("--platform", choices=["kleinanzeigen", "ebay"], default="kleinanzeigen")
    fp = sub.add_parser("flip")
    fp.add_argument("--buy", type=float, required=True)
    fp.add_argument("--sell", type=float, required=True)
    fp.add_argument("--sell-on", choices=["ebay", "kleinanzeigen"], default="ebay")
    fp.add_argument("--hours", type=float, default=0.75)
    fp.add_argument("--shipped-in", action="store_true")
    fp.add_argument("--kleinunternehmer", action="store_true")
    a = p.parse_args()
    print(json.dumps({"laptop": cmd_laptop, "generic": cmd_generic, "flip": cmd_flip}[a.cmd](a), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
