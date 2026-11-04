"""Request/cost accounting for the screening team.

Two separate allowances:
  * free (`:free` model ids): server limit per UTC day (50, or 1000 after >= $10 credits ever bought) and 20/min.
  * paid (e.g. GLM): capped by HUNTBOT_AGENT_MAX_USD_DAY and a per-run request cap.
Every HTTP attempt is logged in `llm_calls`, so the count survives across GitHub Actions runs (cached DB)
and is reconciled with GET /api/v1/key at the start of each run.
"""

from __future__ import annotations

import json
import time
from typing import Literal

from .store import AgentStore


class BudgetExhausted(RuntimeError):
    def __init__(self, msg: str = "budget exhausted", daily: bool = True):
        super().__init__(msg)
        self.daily = daily       # False = only the per-run cap / time cap was hit


def is_free(model_id: str) -> bool:
    return model_id.endswith(":free")


class Budget:
    def __init__(self, store: AgentStore, daily_cap: int | None = None, run_cap: int = 12, run_seconds: int = 300,
                 reserve_interactive: int | None = None, rpm: int = 18, max_calls_run: int = 40,
                 max_usd_day: float = 0.50, always_lenient: bool = False, clock=time.time, sleep=time.sleep):
        self.store, self.clock, self.sleep = store, clock, sleep
        self.daily_cap, self.run_cap, self.run_seconds = daily_cap, run_cap, run_seconds
        self.reserve_cfg, self.rpm = reserve_interactive, rpm
        self.max_calls_run, self.max_usd_day, self.always_lenient = max_calls_run, max_usd_day, always_lenient
        self.run_calls = self.run_free_calls = 0
        self.run_started = clock()

    # -- time helpers
    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def start_run(self) -> None:
        self.run_calls = self.run_free_calls = 0
        self.run_started = self.clock()

    def _q(self, sql: str, *args):
        return self.store.db.conn.execute(sql, args).fetchone()[0] or 0

    # -- server reconciliation
    def refresh(self, client) -> None:
        try:
            info = client.key_info()
        except Exception:
            return       # keep last known values
        fr = info.get("free_model_daily_requests") or {}
        if "limit" in fr:
            self.store.kv_set("or_daily_limit", str(int(fr["limit"])))
            if "remaining" in fr:
                self.store.kv_set("or_remaining_at_refresh", str(int(fr["remaining"])))
                self.store.kv_set("or_refresh_call_id", str(self._q("SELECT COALESCE(MAX(id),0) FROM llm_calls")))
                self.store.kv_set("or_refresh_day", self.today())
        self.store.kv_set("or_key_info", json.dumps({k: info.get(k) for k in
                          ("label", "usage", "usage_daily", "limit", "limit_remaining", "is_free_tier") if k in info}))

    @property
    def daily_limit(self) -> int:
        return int(self.store.kv_get("or_daily_limit", "50") or 50)

    @property
    def tier(self) -> Literal["strict", "lenient"]:
        return "lenient" if self.always_lenient or self.daily_limit > 100 else "strict"

    # -- usage
    def used_today(self) -> int:
        """Free-model requests logged locally today (UTC)."""
        return self._q("SELECT COUNT(*) FROM llm_calls WHERE utc_day=? AND is_free=1", self.today())

    def calls_today(self) -> int:
        return self._q("SELECT COUNT(*) FROM llm_calls WHERE utc_day=?", self.today())

    def usd_today(self) -> float:
        return float(self._q("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE utc_day=?", self.today()))

    def reserve(self) -> int:
        if self.reserve_cfg is not None:
            return self.reserve_cfg
        return 10 if self.daily_limit <= 100 else 150

    def remaining_today(self) -> int:
        cap = self.daily_cap if self.daily_cap is not None else max(0, self.daily_limit - 3)
        left = cap - self.used_today()
        if self.store.kv_get("or_refresh_day") == self.today():
            since = self._q("SELECT COUNT(*) FROM llm_calls WHERE is_free=1 AND id>?",
                            int(self.store.kv_get("or_refresh_call_id", "0") or 0))
            left = min(left, int(self.store.kv_get("or_remaining_at_refresh", "0") or 0) - since)
        return max(0, left)

    def exhausted_today(self) -> bool:
        return self.store.kv_get("or_exhausted_day") == self.today()

    def mark_exhausted(self) -> None:
        self.store.kv_set("or_exhausted_day", self.today())

    def _run_time_ok(self) -> bool:
        return self.clock() - self.run_started < self.run_seconds

    def can_spend_free(self, purpose: str = "pipeline") -> bool:
        if self.exhausted_today():
            return False
        rem = self.remaining_today()
        if purpose == "pipeline":
            return rem - self.reserve() >= 1 and self.run_free_calls < self.run_cap
        return rem >= 1

    def can_spend_paid(self, purpose: str = "pipeline") -> bool:
        if self.usd_today() >= self.max_usd_day:
            return False
        return True

    def run_ok(self, purpose: str = "pipeline") -> bool:
        """Per-run request cap and time cap apply to the unattended pipeline only."""
        return purpose != "pipeline" or (self.run_calls < self.max_calls_run and self._run_time_ok())

    def affordable_chain(self, chain: list[str], purpose: str = "pipeline", allow_paid: bool = True) -> list[str]:
        """Drop chain entries that can't be paid for right now. Raises BudgetExhausted if nothing is left."""
        if not self.run_ok(purpose):
            raise BudgetExhausted("per-run cap reached", daily=False)
        paid_ok = allow_paid and self.can_spend_paid(purpose)
        free_ok = self.can_spend_free(purpose)
        out = [m for m in chain if (free_ok if is_free(m) else paid_ok)]
        if not out:
            free_blocked_by_run = (not self.exhausted_today() and self.remaining_today() - self.reserve() >= 1
                                   and self.run_free_calls >= self.run_cap and purpose == "pipeline")
            raise BudgetExhausted("no budget left for this chain", daily=not free_blocked_by_run)
        return out

    def wait_for_slot(self) -> None:
        for _ in range(3):
            now = self.clock()
            rows = [r[0] for r in self.store.db.conn.execute(
                "SELECT at FROM llm_calls WHERE is_free=1 AND at>? ORDER BY at", (now - 60,))]
            if len(rows) < self.rpm:
                return
            self.sleep(max(0.1, 60 - (now - rows[len(rows) - self.rpm])) + 0.1)

    def record(self, *, role: str, purpose: str, models: list[str], served: str, status: int | None, ok: bool,
               error: str = "", latency_ms: int = 0, usage: dict | None = None, listing_key: str = "",
               cost_usd: float = 0.0) -> None:
        usage = usage or {}
        # A request is "free" if the model that served it is :free, or (when unknown) its primary is.
        free = is_free(served) if served else is_free(models[0])
        now = self.clock()
        self.store.db.conn.execute(
            """INSERT INTO llm_calls (at, utc_day, provider, role, purpose, models_requested, model_served, http_status,
               ok, error, latency_ms, prompt_tokens, completion_tokens, listing_key, cost_usd, is_free)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, self.today(), "openrouter", role, purpose, ",".join(models), served, status, int(ok), error[:300],
             latency_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"), listing_key, cost_usd, int(free)))
        self.store.db.conn.commit()
        self.run_calls += 1
        self.run_free_calls += int(free)
