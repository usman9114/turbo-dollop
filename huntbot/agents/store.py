"""SQLite persistence for the team: LLM call log, cached agent outputs, stored verdicts, small kv."""

from __future__ import annotations

import hashlib
import json
import time

from ..db import DB
from ..models import Listing
from .schemas import Verdict

PROMPT_VERSION = {"reader": "reader-v1", "inspector": "inspector-v1", "skeptic": "skeptic-v1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL, utc_day TEXT NOT NULL, provider TEXT NOT NULL,
  role TEXT NOT NULL, purpose TEXT NOT NULL,
  models_requested TEXT NOT NULL, model_served TEXT DEFAULT '',
  http_status INTEGER, ok INTEGER NOT NULL, error TEXT DEFAULT '',
  latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, listing_key TEXT DEFAULT '',
  cost_usd REAL DEFAULT 0, is_free INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_llm_calls_day ON llm_calls(utc_day);
CREATE TABLE IF NOT EXISTS agent_outputs (
  content_hash TEXT NOT NULL, role TEXT NOT NULL, prompt_version TEXT NOT NULL,
  model_served TEXT NOT NULL, output_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (content_hash, role, prompt_version));
CREATE TABLE IF NOT EXISTS agent_verdicts (
  key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, decision TEXT NOT NULL,
  verdict_json TEXT NOT NULL, priority REAL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


def sha1(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def content_hash(role: str, listing: Listing, extra: str = "") -> str:
    if role == "reader":   # price excluded: a price change re-uses the Reader output
        return sha1(listing.title, listing.description, listing.condition)
    if role == "inspector":
        return sha1(*listing.image_urls[:3])
    return sha1(role, extra)


class AgentStore:
    def __init__(self, db: DB, clock=time.time):
        self.db = db
        self.clock = clock
        db.conn.executescript(SCHEMA)
        # DBs created by an earlier build of llm_calls lack the cost columns.
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(llm_calls)")}
        for col, ddl in (("cost_usd", "REAL DEFAULT 0"), ("is_free", "INTEGER DEFAULT 0")):
            if col not in cols:
                db.conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} {ddl}")

    # kv
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        row = self.db.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        self.db.conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v))
        self.db.conn.commit()

    # outputs
    def get_output(self, h: str, role: str, version: str) -> tuple[str, str] | None:
        row = self.db.conn.execute(
            "SELECT output_json, model_served FROM agent_outputs WHERE content_hash=? AND role=? AND prompt_version=?",
            (h, role, version)).fetchone()
        return (row["output_json"], row["model_served"]) if row else None

    def put_output(self, h: str, role: str, version: str, model: str, output_json: str) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO agent_outputs VALUES (?,?,?,?,?,?)", (h, role, version, model, output_json, self.clock()))
        self.db.conn.commit()

    # verdicts
    def put_verdict(self, key: str, h: str, verdict: Verdict, priority: float = 0.0) -> None:
        now = self.clock()
        old = self.db.conn.execute("SELECT created_at FROM agent_verdicts WHERE key=?", (key,)).fetchone()
        self.db.conn.execute(
            "INSERT OR REPLACE INTO agent_verdicts VALUES (?,?,?,?,?,?,?)",
            (key, h, verdict.decision, verdict.model_dump_json(), priority, old["created_at"] if old else now, now))
        self.db.conn.commit()

    def get_verdict(self, key: str) -> tuple[Verdict, str] | None:
        row = self.db.conn.execute("SELECT verdict_json, content_hash FROM agent_verdicts WHERE key=?", (key,)).fetchone()
        return (Verdict.model_validate_json(row["verdict_json"]), row["content_hash"]) if row else None

    def pending(self, limit: int = 50) -> list[str]:
        return [r["key"] for r in self.db.conn.execute(
            "SELECT key FROM agent_verdicts WHERE decision='PENDING' ORDER BY priority DESC, created_at LIMIT ?", (limit,))]

    def stale_pending(self, older_than_s: float) -> list[str]:
        cutoff = self.clock() - older_than_s
        return [r["key"] for r in self.db.conn.execute(
            "SELECT key FROM agent_verdicts WHERE decision='PENDING' AND created_at < ?", (cutoff,))]

    def pending_count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) AS n FROM agent_verdicts WHERE decision='PENDING'").fetchone()["n"]


def dumps(model) -> str:
    return json.dumps(model.model_dump(), ensure_ascii=False)
