"""SQLite: seen listings (dedup), screenings, rate-limit state and the flip inventory.

Data protection (plan §4.4): no seller names or phone numbers are stored, only the
postcode/area shown on the ad. Unbought listings are purged after 60 days; purchase
and sale records are kept (8 years under §147 AO).
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from hunter.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    price_eur REAL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screenings (
    key TEXT NOT NULL,
    model TEXT NOT NULL,
    created REAL NOT NULL,
    ev REAL,
    alert INTEGER NOT NULL DEFAULT 0,
    assessment TEXT NOT NULL,
    score TEXT NOT NULL,
    PRIMARY KEY (key, model)
);
CREATE TABLE IF NOT EXISTS notified (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_log (
    host TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    host TEXT PRIMARY KEY,
    until REAL NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT,
    title TEXT NOT NULL,
    serial TEXT,
    buy_price REAL NOT NULL,
    bought_at TEXT NOT NULL,
    parts_cost REAL NOT NULL DEFAULT 0,
    other_cost REAL NOT NULL DEFAULT 0,
    labour_hours REAL NOT NULL DEFAULT 0,
    sold_price REAL,
    sold_at TEXT,
    sale_fees REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);
"""

RETENTION_DAYS = 60


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- listings --------------------------------------------------------
    def upsert_listing(self, listing: Listing) -> bool:
        """Insert or refresh. Returns True if the listing is new."""
        now = time.time()
        row = self.db.execute("SELECT 1 FROM listings WHERE key=?", (listing.key,)).fetchone()
        self.db.execute(
            """INSERT INTO listings (key, source, ad_id, url, title, price_eur, first_seen, last_seen, data)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET url=excluded.url, title=excluded.title,
                 price_eur=excluded.price_eur, last_seen=excluded.last_seen, data=excluded.data""",
            (listing.key, listing.source, listing.ad_id, listing.url, listing.title,
             listing.price_eur, now, now, json.dumps(listing.to_dict(), ensure_ascii=False)),
        )
        self.db.commit()
        return row is None

    def get_listing(self, key: str) -> Listing | None:
        row = self.db.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
        return Listing.from_dict(json.loads(row["data"])) if row else None

    # -- screenings ------------------------------------------------------
    def cached_screening(self, key: str, model: str, price: float | None) -> dict | None:
        row = self.db.execute(
            "SELECT s.assessment, l.price_eur FROM screenings s JOIN listings l USING(key) "
            "WHERE s.key=? AND s.model=?",
            (key, model),
        ).fetchone()
        if row is None or row["price_eur"] != price:
            return None  # price changed -> rescreen
        return json.loads(row["assessment"])

    def save_screening(self, key: str, model: str, assessment: dict, score: dict, ev, alert: bool) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO screenings (key, model, created, ev, alert, assessment, score) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, model, time.time(), ev, int(alert), json.dumps(assessment, ensure_ascii=False),
             json.dumps(score, ensure_ascii=False)),
        )
        self.db.commit()

    def was_notified(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone() is not None

    def mark_notified(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO notified (key, ts) VALUES (?,?)", (key, time.time()))
        self.db.commit()

    # -- rate limiting ---------------------------------------------------
    def last_fetch(self, host: str) -> float | None:
        row = self.db.execute("SELECT MAX(ts) AS ts FROM fetch_log WHERE host=?", (host,)).fetchone()
        return row["ts"] if row and row["ts"] else None

    def fetches_since(self, host: str, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM fetch_log WHERE host=? AND ts>=?", (host, since)
        ).fetchone()["n"]

    def log_fetch(self, host: str) -> None:
        self.db.execute("INSERT INTO fetch_log (host, ts) VALUES (?,?)", (host, time.time()))
        self.db.execute("DELETE FROM fetch_log WHERE ts < ?", (time.time() - 7 * 86400,))
        self.db.commit()

    def blocked_until(self, host: str) -> tuple[float, str] | None:
        row = self.db.execute("SELECT until, reason FROM blocks WHERE host=?", (host,)).fetchone()
        if row and row["until"] > time.time():
            return row["until"], row["reason"]
        return None

    def set_block(self, host: str, hours: float, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO blocks (host, until, reason) VALUES (?,?,?)",
            (host, time.time() + hours * 3600, reason),
        )
        self.db.commit()

    # -- inventory -------------------------------------------------------
    def add_purchase(self, title: str, buy_price: float, key: str | None = None,
                     serial: str | None = None, notes: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO inventory (key, title, serial, buy_price, bought_at, notes) VALUES (?,?,?,?,?,?)",
            (key, title, serial, buy_price, _today(), notes),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def add_cost(self, item_id: int, parts: float = 0.0, other: float = 0.0, hours: float = 0.0) -> None:
        self.db.execute(
            "UPDATE inventory SET parts_cost=parts_cost+?, other_cost=other_cost+?, labour_hours=labour_hours+? "
            "WHERE id=?",
            (parts, other, hours, item_id),
        )
        self.db.commit()

    def mark_sold(self, item_id: int, price: float, fees: float = 0.0) -> None:
        self.db.execute(
            "UPDATE inventory SET sold_price=?, sold_at=?, sale_fees=? WHERE id=?",
            (price, _today(), fees, item_id),
        )
        self.db.commit()

    def inventory(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM inventory ORDER BY id").fetchall()

    def export_csv(self, path: Path, vat_mode: str) -> None:
        """Per-item record incl. the §25a margin and VAT, for the tax advisor."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["id", "title", "serial", "bought_at", "buy_price", "parts_cost", "other_cost",
                        "labour_hours", "sold_at", "sold_price", "sale_fees", "margin_25a", "vat_25a",
                        "profit", "profit_per_hour"])
            for r in self.inventory():
                margin = vat = profit = per_h = None
                if r["sold_price"] is not None:
                    margin = max(0.0, r["sold_price"] - r["buy_price"])
                    vat = round(margin * 19 / 119, 2) if vat_mode == "25a" else 0.0
                    profit = round(r["sold_price"] - r["sale_fees"] - vat - r["buy_price"]
                                   - r["parts_cost"] - r["other_cost"], 2)
                    per_h = round(profit / r["labour_hours"], 2) if r["labour_hours"] else None
                w.writerow([r["id"], r["title"], r["serial"] or "", r["bought_at"], r["buy_price"],
                            r["parts_cost"], r["other_cost"], r["labour_hours"], r["sold_at"] or "",
                            r["sold_price"] if r["sold_price"] is not None else "", r["sale_fees"],
                            margin if margin is not None else "", vat if vat is not None else "",
                            profit if profit is not None else "", per_h if per_h is not None else ""])

    # -- retention -------------------------------------------------------
    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        cutoff = time.time() - days * 86400
        bought = "SELECT key FROM inventory WHERE key IS NOT NULL"
        n = self.db.execute(
            f"DELETE FROM listings WHERE last_seen < ? AND key NOT IN ({bought})", (cutoff,)
        ).rowcount
        self.db.execute(f"DELETE FROM screenings WHERE key NOT IN (SELECT key FROM listings)")
        self.db.execute(f"DELETE FROM notified WHERE key NOT IN (SELECT key FROM listings)")
        self.db.commit()
        return n


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()
