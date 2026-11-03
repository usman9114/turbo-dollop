"""Profit ledger, §25a margin log and DAC7 counters from the inventory tables."""

from __future__ import annotations

import csv
import datetime as dt
import io

from .config import Economics
from .db import DB
from .scoring import vat_25a


def ledger(db: DB, econ: Economics) -> list[dict]:
    rows = []
    for r in db.inventory_rows():
        sold = r["sale_price"] is not None
        vat = vat_25a(r["sale_price"], r["buy_price"], econ) if sold else 0.0
        reserve = (r["sale_price"] or 0) * econ.warranty_reserve if sold else 0.0
        profit = ((r["sale_price"] or 0) - (r["sale_fees"] or 0) - (r["sale_shipping"] or 0) - vat - reserve
                  - r["buy_price"] - r["extra_costs"]) if sold else None
        hours = (r["labour_min"] or 0) / 60
        rows.append({
            "id": r["id"],
            "title": r["title"],
            "bought": dt.date.fromtimestamp(r["bought_at"]).isoformat(),
            "buy_price": r["buy_price"],
            "extra_costs": round(r["extra_costs"], 2),
            "sold": dt.date.fromtimestamp(r["sold_at"]).isoformat() if sold else "",
            "sale_price": r["sale_price"],
            "sale_fees": r["sale_fees"],
            "sale_shipping": r["sale_shipping"],
            "margin_25a": round(max(0.0, (r["sale_price"] or 0) - r["buy_price"]), 2) if sold else None,
            "vat_25a": round(vat, 2),
            "warranty_reserve": round(reserve, 2),
            "profit": round(profit, 2) if profit is not None else None,
            "hours": round(hours, 2),
            "per_hour": (f"€{profit / hours:.0f}/h" if profit is not None and hours else "time not logged"),
            "receipt_ok": bool(r["receipt_ok"]),
        })
    return rows


def summary_text(db: DB, econ: Economics) -> str:
    rows = ledger(db, econ)
    sold = [r for r in rows if r["profit"] is not None]
    year = dt.date.today().year
    sales_this_year = [r for r in sold if r["sold"].startswith(str(year))]
    revenue = sum(r["sale_price"] for r in sales_this_year)
    profit = sum(r["profit"] for r in sold)
    hours = sum(r["hours"] for r in sold)
    open_stock = [r for r in rows if r["profit"] is None]
    return (
        f"Flips sold: {len(sold)} · open stock: {len(open_stock)} (€{sum(r['buy_price'] for r in open_stock):.0f} tied up)\n"
        f"Profit (after §25a VAT + 4% reserve): €{profit:.2f}"
        + (f" · €{profit / hours:.0f}/h over {hours:.1f} h" if hours else "") + "\n"
        f"DAC7 {year}: {len(sales_this_year)}/30 sales, €{revenue:.0f}/€2000 revenue\n"
        f"Day-90 gate: ≥15 flips and ≥€25/h; stop if <€10/h."
    )


def ledger_csv(db: DB, econ: Economics) -> str:
    rows = ledger(db, econ)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue()
