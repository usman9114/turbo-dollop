"""The screening team orchestrator: plain code, sequential, early exits. LLMs only read and look."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .. import features as feat_mod
from .. import rules, scoring
from ..config import Settings
from ..db import DB
from ..models import Listing, Score, Screen
from . import gate, ground, judge, merge, prompts
from .budget import Budget, BudgetExhausted
from .openrouter import OpenRouterClient
from .registry import Registry
from .roles import RoleRunner, RoleSpec, specs_from_settings
from .schemas import Verdict
from .store import AgentStore, content_hash, sha1

log = logging.getLogger(__name__)


@dataclass
class TeamOutcome:
    screen: Screen
    score: Score
    verdict: Verdict | None
    model_used: str
    features: feat_mod.Features | None
    calls: int


def short(model_id: str) -> str:
    return model_id.split("/")[-1].removesuffix(":free")


class ScreeningTeam:
    def __init__(self, settings: Settings, db: DB, client: OpenRouterClient, budget: Budget | None = None,
                 registry: Registry | None = None, clock=time.time, sleep=time.sleep,
                 specs: dict[str, RoleSpec] | None = None):
        self.settings, self.db, self.client, self.clock = settings, db, client, clock
        self.store = AgentStore(db, clock)
        self.budget = budget or Budget(
            self.store, daily_cap=settings.agent_daily_cap, run_cap=settings.agent_run_cap,
            run_seconds=settings.agent_run_seconds, reserve_interactive=settings.agent_reserve,
            max_calls_run=settings.agent_max_calls_run, max_usd_day=settings.agent_max_usd_day,
            always_lenient=(settings.agent_profile == "glm"), clock=clock, sleep=sleep)
        self.registry = registry or Registry(self.store, clock)
        self.runner = RoleRunner(client, self.budget, self.store, self.registry, sleep=sleep)
        self.specs = specs or specs_from_settings(settings)
        self.disabled_reason = ""

    # -- run lifecycle -----------------------------------------------------------------
    def start_run(self) -> None:
        self.disabled_reason = ""
        self.runner.fatal = ""
        self.budget.refresh(self.client)
        self.registry.refresh(self.client)
        self.budget.start_run()

    def should_push(self, score: Score, verdict: Verdict | None) -> bool:
        return judge.should_push(score, verdict, self.settings.push_watch, self.settings.team_enforce)

    # -- one listing -------------------------------------------------------------------
    def screen(self, listing: Listing, price_model, purpose: str = "pipeline", cache_only: bool = False) -> TeamOutcome | None:
        econ = self.settings.economics
        rule = rules.screen(listing)
        feats0 = feat_mod.extract(listing.title, listing.description, listing.source, negotiable=listing.negotiable,
                                  condition_field=listing.condition)
        score0 = scoring.score(listing, rule, econ, price_model, feats=feats0)
        rules_out = TeamOutcome(rule, score0, None, "rules", feats0, 0)
        rhash = content_hash("reader", listing)

        if score0.rejected:
            v = judge.decide(listing, rule, feats0, score0, None, None, None, None, [], "final", econ)
            return self._finish(listing, rules_out, v, rhash, 0.0)
        if not gate.is_candidate(listing, score0, self.budget.tier, econ, rule.catalog_key):
            return None if cache_only else rules_out
        prio = gate.priority(listing, score0, rule.catalog_key)

        if self.disabled_reason or self.runner.fatal:
            if cache_only:
                return None
            self.disabled_reason = self.disabled_reason or self.runner.fatal
            return self._unscreened(listing, rules_out, rhash, prio, f"team disabled: {self.disabled_reason}")

        calls0, usd0 = self.runner.calls, self.budget.usd_today()
        models: dict[str, str] = {}
        ran: list[str] = []

        def run(role: str, messages: list[dict], h: str):
            if cache_only:
                cached = self.store.get_output(h, role, self.specs[role].prompt_version)
                if not cached:
                    raise _CacheMiss()
                return self.specs[role].output.model_validate_json(cached[0]), cached[1]
            return self.runner.run(self.specs[role], messages, h, purpose, listing.key)

        # S1 Reader -------------------------------------------------------------------------
        try:
            reader, rmodel = run("reader", prompts.reader_messages(listing), rhash)
        except _CacheMiss:
            return None
        except BudgetExhausted as exc:
            if exc.daily:
                return self._unscreened(listing, rules_out, rhash, prio, "budget exhausted today (rules only)")
            return self._pending(listing, rules_out, rhash, prio)
        if reader is None:
            if self.runner.fatal:
                self.disabled_reason = self.runner.fatal
            return self._unscreened(listing, rules_out, rhash, prio, "reader unavailable (rules only)")
        models["reader"], ran = rmodel, ["reader"]
        g = ground.verify_reader(reader, listing)
        screen1, feats1, soft = merge.apply_reader(rule, feats0, reader, g, listing)
        score1 = scoring.score(listing, screen1, econ, price_model, feats=feats1)

        def ctx(stage: str, screen, score, inspector, skeptic, **kw) -> Verdict:
            return judge.decide(listing, screen, feats1, score, reader, g, inspector, skeptic, soft, stage, econ,
                                models_used=dict(models), agents_run=list(ran), calls_used=self.runner.calls - calls0,
                                cost_usd=self.budget.usd_today() - usd0, **kw)

        final_screen, final_score, inspector, skeptic = screen1, score1, None, None
        v = ctx("reader", screen1, score1, None, None)
        if v.decision != "PASS" and not score1.alert:
            v = ctx("final", screen1, score1, None, None)           # price-less VB or "not a deal after reading"
        elif v.decision != "PASS":
            # S2 Inspector --------------------------------------------------------------------
            images = [u for u in listing.image_urls if u.startswith("https://")]
            inspector_expected = bool(images)
            if images:
                try:
                    inspector, imodel = run("inspector", prompts.inspector_messages(listing), content_hash("inspector", listing))
                except _CacheMiss:
                    return None
                except BudgetExhausted:
                    inspector, imodel = None, ""
                if inspector is not None:
                    models["inspector"] = imodel
                    ran.append("inspector")
                    final_screen, isoft = merge.apply_inspector(screen1, inspector, listing)
                    soft = soft + isoft
                    final_score = scoring.score(listing, final_screen, econ, price_model, feats=feats1)
            v = ctx("inspector", final_screen, final_score, inspector, None, inspector_expected=inspector_expected and inspector is None)
            # S3 Skeptic (only on a would-be BUY) ----------------------------------------------
            if v.decision == "BUY":
                ih = content_hash("inspector", listing) if inspector else ""
                sh = sha1("skeptic", rhash, ih, str(listing.price), f"{final_score.fair_ask or 0:.0f}|{final_score.ev:.0f}")
                try:
                    skeptic, smodel = run("skeptic", prompts.skeptic_messages(listing, reader, inspector, final_score), sh)
                except _CacheMiss:
                    return None
                except BudgetExhausted:
                    skeptic, smodel = None, ""
                if skeptic is not None:
                    models["skeptic"] = smodel
                    ran.append("skeptic")
                v = ctx("final", final_screen, final_score, inspector, skeptic,
                        inspector_expected=inspector_expected and inspector is None, skeptic_expected=True)
            else:
                v = ctx("final", final_screen, final_score, inspector, None,
                        inspector_expected=inspector_expected and inspector is None)

        # enrich with what the card shows
        qs: list[str] = []
        for q in list(reader.questions_for_seller) + (list(skeptic.questions_for_seller) if skeptic else []):
            if q not in qs:
                qs.append(q)
        final_screen = final_screen.model_copy(update={"questions_for_seller": qs[:3]})
        if inspector:
            dmg = f", damage: {', '.join(inspector.visible_damage)}" if inspector.visible_damage else ""
            v.photo = f"{inspector.photo_kind}, {inspector.screen_state}{dmg}"
        if skeptic and skeptic.objections:
            v.objection = skeptic.objections[0].claim
        v.questions = qs[:3]
        model_used = "team:" + "+".join(dict.fromkeys(short(m) for m in models.values())) if models else "rules"
        out = TeamOutcome(final_screen, final_score, v, model_used, feats1, self.runner.calls - calls0)
        return self._finish(listing, out, v, rhash, prio)

    # -- terminal states ------------------------------------------------------------
    def _finish(self, listing, out: TeamOutcome, v: Verdict, rhash: str, prio: float) -> TeamOutcome:
        out.verdict = v
        self.store.put_verdict(listing.key, rhash, v, prio)
        return out

    def _unscreened(self, listing, rules_out: TeamOutcome, rhash, prio, why: str) -> TeamOutcome:
        v = Verdict(decision="UNSCREENED", confidence="low", reasons=[why], degraded=True)
        return self._finish(listing, rules_out, v, rhash, prio)

    def _pending(self, listing, rules_out: TeamOutcome, rhash, prio) -> TeamOutcome:
        v = Verdict(decision="PENDING", confidence="low", reasons=["queued: run cap reached, screened next run"], degraded=True)
        return self._finish(listing, rules_out, v, rhash, prio)

    def mark_pending(self, listing: Listing, score: Score, catalog_key: str) -> None:
        v = Verdict(decision="PENDING", confidence="low", reasons=["queued for the screening team"], degraded=True)
        self.store.put_verdict(listing.key, content_hash("reader", listing), v, gate.priority(listing, score, catalog_key))

    def rescore_cached(self, listing: Listing, price_model) -> TeamOutcome | None:
        """Re-run merge + judge from cached agent outputs (0 calls); None if it needs a model call."""
        stored = self.store.get_verdict(listing.key)
        if stored is None or stored[0].decision in ("PENDING",):
            return None
        return self.screen(listing, price_model, purpose="pipeline", cache_only=True)

    # -- reporting ------------------------------------------------------------------
    def status(self) -> dict:
        b, conn = self.budget, self.db.conn
        roles = {}
        for r in conn.execute("SELECT role, COUNT(*) n, SUM(ok) ok FROM llm_calls WHERE utc_day=? GROUP BY role", (b.today(),)):
            roles[r["role"]] = f"{r['ok'] or 0}/{r['n']} ok"
        errs = [f"{r['role']}: {r['error']}" for r in conn.execute(
            "SELECT role, error FROM llm_calls WHERE ok=0 ORDER BY id DESC LIMIT 5")]
        return {
            "provider": "openrouter", "profile": self.settings.agent_profile, "tier": b.tier,
            "enforce": self.settings.team_enforce, "disabled": self.disabled_reason or None,
            "free_limit": b.daily_limit, "free_used_local": b.used_today(),
            "free_remaining": b.remaining_today(), "reserve": b.reserve(),
            "calls_today": b.calls_today(), "usd_today": round(b.usd_today(), 4), "usd_cap": b.max_usd_day,
            "pending": self.store.pending_count(), "role_success_today": roles,
            "chains": {r: s.chain for r, s in self.specs.items()}, "last_errors": errs + self.runner.errors[-3:],
        }


class _CacheMiss(Exception):
    pass


def build_team(settings: Settings, db: DB) -> ScreeningTeam:
    client = OpenRouterClient(settings.openrouter_key, timeout=settings.or_timeout,
                              allow_paid=settings.or_allow_paid or settings.agent_profile == "glm")
    return ScreeningTeam(settings, db, client)
