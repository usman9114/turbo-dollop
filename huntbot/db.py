"""SQLite storage: listings (+ full-text index), scores, inventory and sales."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Listing, Score, Screen

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    price REAL,
    negotiable INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    location TEXT DEFAULT '',
    shipping INTEGER,
    shipping_cost REAL,
    image_urls TEXT DEFAULT '[]',
    seller_hash TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    raw_source TEXT DEFAULT '',
    fingerprint TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT DEFAULT 'new'          -- new | notified | passed | bought | sold
);
CREATE INDEX IF NOT EXISTS idx_listings_fp ON listings(fingerprint);
CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(key UNINDEXED, title, description, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS scores (
    key TEXT PRIMARY KEY REFERENCES listings(key),
    screen TEXT NOT NULL,
    score TEXT NOT NULL,
    ev REAL NOT NULL,
    alert INTEGER NOT NULL,
    model_used TEXT DEFAULT 'rules',
    scored_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT,
    title TEXT NOT NULL,
    buy_price REAL NOT NULL,
    bought_at REAL NOT NULL,
    serial TEXT DEFAULT '',
    receipt_ok INTEGER DEFAULT 0,       -- purchase receipt + ownership declaration on file (§25a, §259 StGB)
    sale_price REAL,
    sale_fees REAL,
    sale_shipping REAL,
    sold_at REAL,
    channel TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_id INTEGER NOT NULL REFERENCES inventory(id),
    kind TEXT NOT NULL,                 -- parts | travel | labour_min | other
    amount REAL NOT NULL,
    note TEXT DEFAULT '',
    at REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- listings -------------------------------------------------------
    def upsert_listing(self, listing: Listing, fingerprint: str) -> tuple[bool, bool]:
        """Returns (is_new, changed). A repost with identical text under a new id counts as not new."""
        now = time.time()
        row = self.conn.execute("SELECT price, description FROM listings WHERE key=?", (listing.key,)).fetchone()
        if row is None:
            dup = self.conn.execute(
                "SELECT key FROM listings WHERE fingerprint=? AND key<>?", (fingerprint, listing.key)
            ).fetchone()
            self.conn.execute(
                """INSERT INTO listings (key, source, ad_id, title, url, price, negotiable, description, location,
                   shipping, shipping_cost, image_urls, seller_hash, condition, raw_source, fingerprint, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (listing.key, listing.source, listing.ad_id, listing.title, listing.url, listing.price,
                 int(listing.negotiable), listing.description, listing.location,
                 None if listing.shipping is None else int(listing.shipping), listing.shipping_cost,
                 json.dumps(listing.image_urls), listing.seller_hash, listing.condition, listing.raw_source,
                 fingerprint, now, now),
            )
            self.conn.execute("INSERT INTO listings_fts (key, title, description) VALUES (?,?,?)",
                              (listing.key, listing.title, listing.description))
            self.conn.commit()
            return dup is None, True
        # Existing: richer data (e.g. companion page with full description) replaces thin email data.
        changed = (row["price"] != listing.price) or (len(listing.description) > len(row["description"] or ""))
        if changed:
            self.conn.execute(
                """UPDATE listings SET title=?, price=?, negotiable=?, description=?, location=COALESCE(NULLIF(?, ''), location),
                   shipping=COALESCE(?, shipping), image_urls=CASE WHEN ?='[]' THEN image_urls ELSE ? END, last_seen=? WHERE key=?""",
                (listing.title, listing.price, int(listing.negotiable),
                 listing.description if len(listing.description) > len(row["description"] or "") else row["description"],
                 listing.location, None if listing.shipping is None else int(listing.shipping),
                 json.dumps(listing.image_urls), json.dumps(listing.image_urls), now, listing.key),
            )
            self.conn.execute("UPDATE listings_fts SET title=?, description=? WHERE key=?",
                              (listing.title, listing.description, listing.key))
        else:
            self.conn.execute("UPDATE listings SET last_seen=? WHERE key=?", (now, listing.key))
        self.conn.commit()
        return False, changed

    def get_listing(self, key: str) -> Listing | None:
        row = self.conn.execute("SELECT * FROM listings WHERE key=?", (key,)).fetchone()
        return _row_to_listing(row) if row else None

    def set_status(self, key: str, status: str) -> None:
        self.conn.execute("UPDATE listings SET status=? WHERE key=?", (status, key))
        self.conn.commit()

    def status(self, key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM listings WHERE key=?", (key,)).fetchone()
        return row["status"] if row else None

    def search(self, match: str, max_price: float | None = None, limit: int = 200) -> list[Listing]:
        if not match:
            return []
        sql = """SELECT l.* FROM listings_fts f JOIN listings l ON l.key = f.key
                 WHERE listings_fts MATCH ? AND l.status NOT IN ('passed')"""
        args: list = [match]
        if max_price is not None:
            sql += " AND (l.price IS NULL OR l.price <= ?)"
            args.append(max_price)
        sql += " ORDER BY l.last_seen DESC LIMIT ?"
        args.append(limit)
        return [_row_to_listing(r) for r in self.conn.execute(sql, args)]

    # ---- scores ---------------------------------------------------------
    def save_score(self, key: str, screen: Screen, score: Score, model_used: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scores (key, screen, score, ev, alert, model_used, scored_at) VALUES (?,?,?,?,?,?,?)",
            (key, screen.model_dump_json(), json.dumps(score.as_dict()), score.ev, int(score.alert), model_used, time.time()),
        )
        self.conn.commit()

    def get_score(self, key: str) -> tuple[Screen, dict, str] | None:
        row = self.conn.execute("SELECT screen, score, model_used FROM scores WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return Screen.model_validate_json(row["screen"]), json.loads(row["score"]), row["model_used"]

    def top(self, limit: int = 50, alerts_only: bool = False) -> list[tuple[Listing, dict]]:
        sql = """SELECT l.*, s.score AS score_json FROM listings l JOIN scores s ON s.key = l.key
                 WHERE l.status NOT IN ('passed', 'sold')"""
        if alerts_only:
            sql += " AND s.alert = 1"
        sql += " ORDER BY s.ev DESC LIMIT ?"
        return [(_row_to_listing(r), json.loads(r["score_json"])) for r in self.conn.execute(sql, (limit,))]

    # ---- inventory ------------------------------------------------------
    def add_purchase(self, title: str, buy_price: float, listing_key: str | None = None, serial: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO inventory (listing_key, title, buy_price, bought_at, serial) VALUES (?,?,?,?,?)",
            (listing_key, title, buy_price, time.time(), serial),
        )
        if listing_key:
            self.conn.execute("UPDATE listings SET status='bought' WHERE key=?", (listing_key,))
        self.conn.commit()
        return int(cur.lastrowid)

    def add_cost(self, inventory_id: int, kind: str, amount: float, note: str = "") -> None:
        self.conn.execute("INSERT INTO costs (inventory_id, kind, amount, note, at) VALUES (?,?,?,?,?)",
                          (inventory_id, kind, amount, note, time.time()))
        self.conn.commit()

    def record_sale(self, inventory_id: int, price: float, fees: float = 0.0, shipping: float = 0.0, channel: str = "") -> None:
        self.conn.execute(
            "UPDATE inventory SET sale_price=?, sale_fees=?, sale_shipping=?, sold_at=?, channel=? WHERE id=?",
            (price, fees, shipping, time.time(), channel, inventory_id),
        )
        row = self.conn.execute("SELECT listing_key FROM inventory WHERE id=?", (inventory_id,)).fetchone()
        if row and row["listing_key"]:
            self.conn.execute("UPDATE listings SET status='sold' WHERE key=?", (row["listing_key"],))
        self.conn.commit()

    def inventory_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT i.*,
                 COALESCE((SELECT SUM(amount) FROM costs c WHERE c.inventory_id=i.id AND c.kind<>'labour_min'), 0) AS extra_costs,
                 COALESCE((SELECT SUM(amount) FROM costs c WHERE c.inventory_id=i.id AND c.kind='labour_min'), 0) AS labour_min
               FROM inventory i ORDER BY i.bought_at"""))

    # ---- retention ------------------------------------------------------
    def purge(self, days: int = 60) -> int:
        """Delete listings never bought, older than `days` (purchase records are kept: §147 AO)."""
        cutoff = time.time() - days * 86400
        keys = [r["key"] for r in self.conn.execute(
            "SELECT key FROM listings WHERE last_seen < ? AND status NOT IN ('bought', 'sold')", (cutoff,))]
        for key in keys:
            self.conn.execute("DELETE FROM scores WHERE key=?", (key,))
            self.conn.execute("DELETE FROM listings_fts WHERE key=?", (key,))
            self.conn.execute("DELETE FROM listings WHERE key=?", (key,))
        self.conn.commit()
        return len(keys)


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["source"], ad_id=row["ad_id"], title=row["title"], url=row["url"], price=row["price"],
        negotiable=bool(row["negotiable"]), description=row["description"] or "", location=row["location"] or "",
        shipping=None if row["shipping"] is None else bool(row["shipping"]), shipping_cost=row["shipping_cost"],
        image_urls=json.loads(row["image_urls"] or "[]"), seller_hash=row["seller_hash"] or "",
        condition=row["condition"] or "", raw_source=row["raw_source"] or "",
    )
