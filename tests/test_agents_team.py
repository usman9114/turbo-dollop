"""End-to-end screening-team tests (Pipeline + ScreeningTeam) with a scripted OpenRouter. No network."""

import httpx2 as httpx
import pytest

from huntbot import features
from huntbot.agents.budget import Budget
from huntbot.agents.openrouter import OpenRouterClient
from huntbot.agents.registry import Registry
from huntbot.agents.store import AgentStore
from huntbot.agents.team import ScreeningTeam
from huntbot.config import Settings
from huntbot.db import DB
from huntbot.pipeline import Pipeline
from huntbot.telegram import card

from .agents_fakes import (DESC, TITLE, Clock, FakeOR, completion, inspector_json, listing, make, reader_json,
                           skeptic_json)


def test_rule_rejected_costs_zero_calls(tmp_path):
    pipe, team, fake, _ = make(tmp_path)
    r = pipe.process(listing(price=20, title="Lenovo ThinkPad T14 Gen 1 Tastatur – Deutsch QWERTZ", desc="Tastatur, QWERTZ."))
    assert fake.count == 0
    assert r.verdict.decision == "PASS" and r.score.rejected


def test_reader_part_with_verified_quote_is_pass_and_skips_inspector(tmp_path):
    desc = "Verkaufe die Tastatur für ThinkPad T14 Gen 2. Funktioniert einwandfrei. Privatverkauf."
    fake = FakeOR(reader=reader_json(item_type="part", item_type_evidence=["Verkaufe die Tastatur für ThinkPad"]))
    pipe, *_ = make(tmp_path, fake)
    r = pipe.process(listing(desc=desc))
    assert r.verdict.decision == "PASS" and "reader:part" in r.verdict.vetoes
    assert fake.roles == ["reader"]


def test_unverified_quote_never_causes_pass(tmp_path):
    desc = "Verkaufe die Tastatur für ThinkPad T14 Gen 2. Funktioniert einwandfrei. Privatverkauf."
    fake = FakeOR(reader=reader_json(item_type="part", item_type_evidence=["Das hat der Verkäufer nie geschrieben"]))
    pipe, *_ = make(tmp_path, fake)
    r = pipe.process(listing(desc=desc))
    assert r.verdict.decision == "WATCH"


def test_reader_broken_beats_everything(tmp_path):
    desc = "Lenovo T480. Beim Einschalten bleibt alles schwarz, nur der Lüfter dreht. Privatverkauf, keine Garantie."
    fake = FakeOR(reader=reader_json(working_status="broken", working_evidence=["bleibt alles schwarz"],
                                     fault_category="mainboard", device_model="ThinkPad T480"))
    pipe, *_ = make(tmp_path, fake)
    r = pipe.process(listing(price=40, title="Lenovo ThinkPad T480 i5-8350U 8GB 256GB", desc=desc))
    assert r.score.mode == "repair" and r.screen.fault_category == "mainboard"
    assert r.verdict.decision == "PASS" and fake.roles == ["reader"]       # repair EV at €40 is no deal


def test_stated_price_the_regex_misses_caps_fair_ask(tmp_path):
    desc = DESC + " Habe ihn im Februar für 300 Euro neu gekauft."
    assert features.extract(TITLE, desc).retail_price is None          # regex alone does not see it
    fake = FakeOR(reader=reader_json(stated_purchase_price_eur=300.0, stated_purchase_date="2026-02",
                                     purchase_evidence=["für 300 Euro neu gekauft"]))
    pipe, *_ = make(tmp_path, fake)
    r = pipe.process(listing(desc=desc))
    t = pipe.team.screen(r.listing, pipe.model)
    assert t.features.retail_price == 300
    assert t.score.fair_ask <= 300 * 1.08 + 0.01


def test_dealer_ab_price_is_pass(tmp_path):
    desc = "Refurbished ThinkPad T14, ab 290€ in verschiedenen Konfigurationen. Unsere Vorteile: 12 Monate Garantie."
    fake = FakeOR(reader=reader_json(price_is_for_this_item=False, price_evidence=["ab 290€"],
                                     seller_type="dealer", seller_evidence=["Unsere Vorteile"]))
    pipe, *_ = make(tmp_path, fake)
    r = pipe.process(listing(price=150, desc=desc))
    assert r.score.rejected is False and "dealer_listing" in r.screen.red_flags       # rules flag
    assert r.verdict.decision == "PASS"


def test_inspector_bios_prompt_halves_ev_and_activation_lock_rejects(tmp_path):
    base = make(tmp_path, FakeOR(skeptic=skeptic_json()), db_name="a.db")[0].process(listing())
    pipe, *_ = make(tmp_path, FakeOR(inspector=inspector_json(screen_state="bios_password_prompt", confidence="medium")), db_name="b.db")
    r = pipe.process(listing())
    assert "bios_password" in r.screen.red_flags and r.score.ev == pytest.approx(base.score.ev * 0.5, rel=0.05)
    pipe, *_ = make(tmp_path, FakeOR(inspector=inspector_json(screen_state="organisation_or_activation_lock", confidence="high")), db_name="c.db")
    r = pipe.process(listing())
    assert r.score.rejected and r.verdict.decision == "PASS"


def test_full_happy_path_is_buy_with_three_requests(tmp_path):
    notified = []
    pipe, team, fake, _ = make(tmp_path, notified=notified)
    r = pipe.process(listing())
    assert r.verdict.decision == "BUY" and r.verdict.agents_run == ["reader", "inspector", "skeptic"]
    assert fake.roles == ["reader", "inspector", "skeptic"]
    assert len(notified) == 1 and notified[0].verdict.decision == "BUY"
    assert fake.requests[0]["messages"][1]["content"].count("Funktioniert einwandfrei") == 1


def test_pii_is_stripped_before_the_llm(tmp_path):
    pipe, team, fake, _ = make(tmp_path)
    pipe.process(listing(desc=DESC + " Ruf an: 0172 1234567 oder max@example.com"))
    sent = fake.requests[0]["messages"][1]["content"]
    assert "1234567" not in sent and "example.com" not in sent and "[telefon]" in sent


def test_skeptic_veto_verified_is_pass_unverified_is_watch(tmp_path):
    obj = lambda ev: [{"kind": "broken", "claim": "device may be dead", "evidence": ev, "severity": "high"}]
    pipe, *_ = make(tmp_path, FakeOR(skeptic=skeptic_json(verdict="veto", objections=obj("Funktioniert einwandfrei"))), db_name="a.db")
    assert pipe.process(listing()).verdict.decision == "PASS"
    pipe, *_ = make(tmp_path, FakeOR(skeptic=skeptic_json(verdict="veto", objections=obj("a quote that is not in the ad"))), db_name="b.db")
    assert pipe.process(listing()).verdict.decision == "WATCH"


def test_thin_listing_is_watch_pushed_never_buy(tmp_path):
    notified = []
    pipe, team, fake, _ = make(tmp_path, notified=notified)
    r = pipe.process(listing(desc=""))
    assert r.verdict.decision == "WATCH" and "thin listing" in r.verdict.reasons[0]
    assert "skeptic" not in fake.roles
    # whether it alerts depends on the score; with no description the title alone must not give BUY
    assert r.verdict.decision != "BUY"


def test_budget_exhausted_is_unscreened_and_pushed(tmp_path):
    notified = []
    pipe, team, fake, clock = make(tmp_path, notified=notified, agent_profile="free")
    team.budget.mark_exhausted()
    r = pipe.process(listing())
    assert fake.count == 0 and r.verdict.decision == "UNSCREENED"
    assert r.score.alert and len(notified) == 1
    assert "UNSCREENED" in card(notified[0])[0].split("\n")[0]


def test_cache_second_pass_free_and_price_change_reuses_reader(tmp_path):
    pipe, team, fake, _ = make(tmp_path)
    pipe.process(listing())
    n = fake.count
    again = pipe.process(listing())
    assert fake.count == n and again.verdict.decision == "BUY"
    pipe.process(listing(price=140))              # price changed: Reader/Inspector cached, Skeptic re-asked
    assert fake.roles.count("reader") == 1 and fake.roles.count("inspector") == 1 and fake.roles.count("skeptic") == 2


def test_run_cap_makes_pending_then_drain_and_stale_promotion(tmp_path):
    notified = []
    pipe, team, fake, clock = make(tmp_path, notified=notified, agent_max_calls_run=3)
    team.budget.start_run()
    a = pipe.process(listing(ad_id="1"))
    b = pipe.process(listing(ad_id="2", title=TITLE + " B", desc=DESC + " Zweites Gerät."))
    assert a.verdict.decision == "BUY" and b.verdict.decision == "PENDING"
    assert len(notified) == 1                                   # the pending one is not pushed
    team.start_run()                                            # next run
    assert pipe.drain_pending() == 1
    assert pipe.team.store.get_verdict("kleinanzeigen:2")[0].decision == "BUY" and len(notified) == 2
    # stale promotion: queue another one, never drain it, advance the clock 46 min
    team.budget.run_calls = 99
    c = pipe.process(listing(ad_id="3", title=TITLE + " C", desc=DESC + " Drittes Gerät."))
    assert c.verdict.decision == "PENDING" and c.score.alert is False
    clock.t += 46 * 60
    notified.clear()
    pipe.drain_pending()
    assert pipe.team.store.get_verdict("kleinanzeigen:3")[0].decision == "UNSCREENED"
    assert len(notified) == 1 and notified[0].verdict.decision == "UNSCREENED"


def test_registry_drops_missing_and_expiring_models(tmp_path):
    soon = "2026-11-05"
    models = [{"id": "z-ai/glm-5.3-flash", "architecture": {"input_modalities": ["text", "image"]},
               "supported_parameters": ["structured_outputs"], "pricing": {"prompt": "0.00000003", "completion": "0.00000008"}},
              {"id": "z-ai/glm-4.7-flash", "architecture": {"input_modalities": ["text"]}, "supported_parameters": [],
               "expiration_date": soon, "pricing": {}},
              {"id": "google/gemma-4-31b-it:free", "architecture": {"input_modalities": ["text", "image"]},
               "supported_parameters": ["response_format"], "pricing": {"prompt": "0", "completion": "0"}}]
    clock = Clock(1_793_000_000.0)    # 2026-10-26: soon is 10 days away -> keep; move to 2026-11-04
    clock.t = 1_793_750_400.0
    fake = FakeOR(models=models, key_info={"free_model_daily_requests": {"used": 0, "limit": 1000, "remaining": 1000}})
    pipe, team, *_ = make(tmp_path, fake, clock)
    team.start_run()
    assert team.registry.live_chain(team.specs["reader"].chain, False) == ["z-ai/glm-5.3-flash", "google/gemma-4-31b-it:free"]
    assert team.registry.live_chain(team.specs["inspector"].chain, True) == ["z-ai/glm-5.3-flash", "google/gemma-4-31b-it:free"]
    # nothing live that sees: Inspector unavailable -> BUY capped at WATCH
    team.specs["inspector"].chain = ["z-ai/glm-4.7-flash"]
    r = pipe.process(listing())
    assert r.verdict.decision == "WATCH" and "photos not checked" in r.verdict.reasons


def test_shadow_mode_pushes_like_today(tmp_path):
    notified = []
    desc = "Verkaufe die Tastatur für ThinkPad T14 Gen 2. Funktioniert einwandfrei. Privatverkauf."
    fake = FakeOR(reader=reader_json(item_type="part", item_type_evidence=["Verkaufe die Tastatur für ThinkPad"]))
    pipe, *_ = make(tmp_path, fake, notified=notified, team_enforce=False)
    r = pipe.process(listing(desc=desc))
    assert r.verdict.decision == "PASS"
    assert len(notified) == 0                       # PASS also rejects the item by flag, so the score alert is off too
    # a verdict that says PASS while the score alerts (skeptic veto) is still pushed in shadow mode
    obj = [{"kind": "broken", "claim": "x", "evidence": "Funktioniert einwandfrei", "severity": "high"}]
    pipe, *_ = make(tmp_path, FakeOR(skeptic=skeptic_json(verdict="veto", objections=obj)), notified=notified, team_enforce=False, db_name="s.db")
    r = pipe.process(listing())
    assert r.verdict.decision == "PASS" and r.score.alert and len(notified) == 1
    pipe, *_ = make(tmp_path, FakeOR(skeptic=skeptic_json(verdict="veto", objections=obj)), notified=notified, team_enforce=True, db_name="e.db")
    assert pipe.process(listing(ad_id="9")).score.alert is False              # enforce: not pushed


def test_defaults_without_keys_use_rules_only(tmp_path):
    s = Settings()
    assert s.resolved_provider == "rules" and s.team_enforce is False
    p = Pipeline(s, DB(tmp_path / "d.db"))
    assert p.team is None and p.screener is None
    assert Settings(openrouter_key="k").resolved_provider == "team"
    assert Settings(anthropic_key_present=True).resolved_provider == "claude"
    assert Settings(openrouter_key="k", anthropic_key_present=True, screen_provider="anthropic").resolved_provider == "claude"
    assert Settings(openrouter_key="k", screen_provider="rules").resolved_provider == "rules"
    assert Settings(openrouter_key="k", llm_enabled=False).use_llm is False


def test_claude_path_still_used_with_injected_screener(tmp_path):
    class FakeScreener:
        screen_model = review_model = "fake"
        def screen(self, l): return None
        def review(self, l): return None
    p = Pipeline(Settings(), DB(tmp_path / "c.db"), screener=FakeScreener())
    assert p.team is None
    assert p.process(listing()).verdict is None


def test_fatal_auth_error_disables_team_and_falls_back(tmp_path):
    bad = httpx.Response(401, json={"error": {"message": "invalid key"}})
    fake = FakeOR(reader=[bad])
    pipe, team, *_ = make(tmp_path, fake)
    r = pipe.process(listing())
    assert r.verdict.decision == "UNSCREENED" and team.disabled_reason
    n = fake.count
    pipe.process(listing(ad_id="5", title=TITLE + " X"))
    assert fake.count == n                              # no more calls this run


def test_verdict_in_as_dict_and_cards(tmp_path):
    pipe, *_ = make(tmp_path)
    r = pipe.process(listing())
    assert r.as_dict()["verdict"]["decision"] == "BUY"
    text = card(r)[0]
    assert text.split("\n")[0].startswith("✅ <b>BUY</b>") and "📷 real_device" in text
    pipe, *_ = make(tmp_path, FakeOR(reader=reader_json(working_status="untested", working_evidence=["Funktioniert einwandfrei"])), db_name="w.db")
    w = pipe.process(listing())
    assert w.verdict.decision == "WATCH" and card(w)[0].split("\n")[0].startswith("🔎 <b>WATCH</b>")


def test_process_many_budget_marks_rest_pending_and_interactive_ignores_run_cap(tmp_path):
    pipe, team, fake, _ = make(tmp_path, agent_max_calls_run=0)
    ls = [listing(ad_id="1"), listing(ad_id="2", title=TITLE + " B", desc=DESC + " Zweites.")]
    team.budget.start_run()
    out = pipe.process_many(ls, llm_budget=1, purpose="interactive")       # run cap is for the unattended pipeline only
    decisions = sorted(r.verdict.decision if r.verdict else "-" for r in out)
    assert fake.count > 0 and "BUY" in decisions
    assert team.store.pending_count() == 1                                  # the one outside llm_budget waits for the drain
    again = pipe.process_many(ls, llm_budget=1, purpose="interactive")     # screened one is cached: 0 extra calls
    n = fake.count
    pipe.process_many(ls, llm_budget=0, purpose="interactive")
    assert fake.count == n and any(r.verdict and r.verdict.decision == "BUY" for r in again)


def test_web_badge_carries_the_verdict(tmp_path):
    from huntbot.web import _badge
    pipe, *_ = make(tmp_path)
    b = _badge(pipe.process(listing()))
    assert b["verdict"] == "BUY" and b["verdict_reason"].startswith("deal")
