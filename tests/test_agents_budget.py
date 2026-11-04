import pytest

from huntbot.agents.budget import Budget, BudgetExhausted
from huntbot.agents.store import AgentStore
from huntbot.db import DB

from .agents_fakes import Clock, FakeOR


def mk(tmp_path, clock=None, name="b.db", **kw):
    clock = clock or Clock()
    store = AgentStore(DB(tmp_path / name), clock)
    return Budget(store, clock=clock, sleep=clock.sleep, **kw), clock


def rec(b, n=1, model="x/y:free", cost=0.0, status=200):
    for _ in range(n):
        b.record(role="reader", purpose="pipeline", models=[model], served=model, status=status, ok=True, cost_usd=cost)


def test_usage_survives_reopen_and_day_rollover(tmp_path):
    b, clock = mk(tmp_path)
    rec(b, 4)
    b2, _ = mk(tmp_path, clock)
    assert b2.used_today() == 4
    clock.t += 86400
    assert b2.used_today() == 0


def test_run_cap_and_seconds_stop_pipeline_not_interactive(tmp_path):
    b, clock = mk(tmp_path, run_cap=2, run_seconds=100, max_calls_run=5)
    b.start_run()
    assert b.can_spend_free("pipeline")
    rec(b, 2)
    assert not b.can_spend_free("pipeline") and b.can_spend_free("interactive")
    with pytest.raises(BudgetExhausted) as e:
        b.affordable_chain(["x/y:free"], "pipeline")
    assert e.value.daily is False
    assert b.affordable_chain(["x/y:free"], "interactive") == ["x/y:free"]
    b.start_run()
    clock.t += 101
    assert not b.run_ok("pipeline") and b.run_ok("interactive")


def test_reserve_is_kept_for_interactive(tmp_path):
    b, _ = mk(tmp_path, run_cap=1000)       # limit 50 -> cap 47, reserve 10
    rec(b, 37)
    assert not b.can_spend_free("pipeline") and b.can_spend_free("interactive") and b.can_spend_free("eval")
    rec(b, 10)
    assert not b.can_spend_free("interactive")


def test_rpm_limiter_sleeps(tmp_path):
    b, clock = mk(tmp_path, rpm=18)
    rec(b, 18)
    b.wait_for_slot()
    assert clock.sleeps and 59 <= clock.sleeps[0] <= 61
    clock.sleeps.clear()
    b.wait_for_slot()
    assert clock.sleeps == []


def test_refresh_sets_tier_and_failure_keeps_values(tmp_path):
    b, _ = mk(tmp_path)
    assert b.tier == "strict" and b.daily_limit == 50
    b.refresh(FakeOR(key_info={"free_model_daily_requests": {"used": 5, "limit": 1000, "remaining": 995}}).client())
    assert b.tier == "lenient" and b.daily_limit == 1000 and b.reserve() == 150
    b.refresh(FakeOR(key_info=None).client())                      # HTTP 500: keep last known
    assert b.daily_limit == 1000
    b.refresh(FakeOR(key_info={"free_model_daily_requests": {"used": 49, "limit": 50, "remaining": 1}}).client())
    assert b.tier == "strict" and b.remaining_today() == 1
    rec(b, 1)
    assert b.remaining_today() == 0                                # local calls since the refresh are subtracted


def test_glm_profile_is_always_lenient(tmp_path):
    b, _ = mk(tmp_path, always_lenient=True)
    assert b.tier == "lenient"


def test_paid_spend_cap_drops_paid_models_but_keeps_free_fallbacks(tmp_path):
    b, _ = mk(tmp_path, max_usd_day=0.50)
    chain = ["z-ai/glm-5.3-flash", "google/gemma-4-31b-it:free"]
    assert b.affordable_chain(chain) == chain
    rec(b, 1, model="z-ai/glm-5.3-flash", cost=0.6)
    assert b.affordable_chain(chain) == ["google/gemma-4-31b-it:free"]
    assert b.used_today() == 0                                    # paid calls don't use the free quota
    rec(b, 1, model="z-ai/glm-5.3-flash", cost=0.0)
    b.store.db.conn.execute("UPDATE llm_calls SET model_served='google/gemma-4-31b-it:free', is_free=1 WHERE id=2")
    assert b.used_today() == 1                                    # paid primary served by a :free fallback counts as free
    with pytest.raises(BudgetExhausted):
        b.affordable_chain(["z-ai/glm-5.3"])
