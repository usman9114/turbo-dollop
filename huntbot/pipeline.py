"""Ingest → dedup → screen (rules, then Claude) → score → store → notify."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from . import query as q
from . import rules, scoring
from .config import Settings
from .db import DB
from .agents.schemas import Verdict
from .llm import Screener
from .models import Listing, Score, Screen
from .privacy import strip_pii, text_fingerprint

log = logging.getLogger(__name__)


@dataclass
class Result:
    listing: Listing
    screen: Screen
    score: Score
    model_used: str
    is_new: bool
    verdict: "Verdict | None" = None

    def as_dict(self) -> dict:
        from .offers import draft_offer
        return {
            "key": self.listing.key,
            "source": self.listing.source,
            "title": self.listing.title,
            "url": self.listing.url,
            "price": self.listing.price,
            "negotiable": self.listing.negotiable,
            "location": self.listing.location,
            "image": self.listing.image_urls[0] if self.listing.image_urls else None,
            "screen": self.screen.model_dump(),
            "score": self.score.as_dict(),
            "model_used": self.model_used,
            "verdict": self.verdict.model_dump() if self.verdict else None,
            "offer": draft_offer(self.listing, self.screen, self.score),
        }


class Pipeline:
    def __init__(self, settings: Settings, db: DB, screener: Screener | None = None,
                 notifier: Callable[[Result], None] | None = None, team=None):
        self.settings = settings
        self.db = db
        self.econ = settings.economics
        self.screener = screener
        self.team = team
        if self.screener is None and self.team is None:
            provider = settings.resolved_provider
            if provider == "team":
                from .agents import build_team
                self.team = build_team(settings, db)
            elif provider == "claude":
                self.screener = Screener(settings.screen_model, settings.review_model)
        self.notifier = notifier
        self.model = None
        self._seen_since_fit = 0
        self.refit()

    def refit(self) -> None:
        """Retrain the price model on everything stored (cheap: pure-Python ridge, ~35 features)."""
        from .pricemodel import fit_from_db
        self.model = fit_from_db(self.db)
        self._seen_since_fit = 0

    # -- scoring ----------------------------------------------------------
    def _screen(self, listing: Listing, use_llm: bool, purpose: str = "pipeline") -> tuple[Screen, Score, str, Verdict | None]:
        if use_llm and self.team:
            out = self.team.screen(listing, self.model, purpose)
            return out.screen, out.score, out.model_used, out.verdict
        screen, result, model_used = self._screen_claude(listing, use_llm)
        return screen, result, model_used, None

    def _screen_claude(self, listing: Listing, use_llm: bool) -> tuple[Screen, Score, str]:
        rule = rules.screen(listing)
        best, model_used = rule, "rules"
        result = scoring.score(listing, rule, self.econ, self.model)
        if not (use_llm and self.screener) or result.rejected:
            return best, result, model_used

        first = self.screener.screen(listing)
        if first:
            best, model_used = rules.merge(rule, first), self.screener.screen_model
            result = scoring.score(listing, best, self.econ, self.model)
            escalate = (result.ev >= self.econ.min_ev) or (first.confidence == "low" and result.ev > 0)
            if escalate and not result.rejected:
                second = self.screener.review(listing)
                if second:
                    best, model_used = rules.merge(rule, second), self.screener.review_model
                    result = scoring.score(listing, best, self.econ, self.model)
        return best, result, model_used

    def process(self, listing: Listing, use_llm: bool = True, notify: bool = True, purpose: str = "pipeline") -> Result:
        listing.description = strip_pii(listing.description)
        is_new, changed = self.db.upsert_listing(listing, text_fingerprint(listing.title, listing.description))
        if is_new:
            self._seen_since_fit += 1
            if self._seen_since_fit >= 25:
                self.refit()
        stored = self.db.get_listing(listing.key) or listing
        cached = self.db.get_score(listing.key)
        if cached and not changed:
            screen, _, model_used = cached
            verdict = None
            if self.team and use_llm:
                prior = self.team.store.get_verdict(listing.key)
                if prior and prior[0].decision == "PENDING":
                    cached = None            # queued earlier: screen it now
                else:
                    out = self.team.rescore_cached(stored, self.model)      # 0 calls
                    if out is not None:
                        screen, score, model_used, verdict = out.screen, out.score, out.model_used, out.verdict
                    else:
                        verdict = prior[0] if prior else None
                        score = scoring.score(stored, screen, self.econ, self.model)
                    if verdict is not None:
                        score.alert = self.team.should_push(score, verdict)
                    self.db.save_score(listing.key, screen, score, model_used)
                    return Result(stored, screen, score, model_used, is_new, verdict)
            if cached:
                score = scoring.score(stored, screen, self.econ, self.model)   # re-price with the current model
                self.db.save_score(listing.key, screen, score, model_used)
                prior = self.team.store.get_verdict(listing.key) if self.team else None   # shown, not re-decided
                return Result(stored, screen, score, model_used, is_new, prior[0] if prior else None)

        screen, score, model_used, verdict = self._screen(stored, use_llm, purpose)
        if verdict is not None:
            score.alert = self.team.should_push(score, verdict)
            if verdict.reasons:
                score.reasons.insert(0, f"team {verdict.decision}: {verdict.reasons[0]}")
        self.db.save_score(listing.key, screen, score, model_used)
        result = Result(stored, screen, score, model_used, is_new, verdict)

        if notify and score.alert and self.notifier and self.db.status(listing.key) == "new":
            try:
                self.notifier(result)
                self.db.set_status(listing.key, "notified")
            except Exception:  # notification failure must not lose the listing
                log.exception("notify failed for %s", listing.key)
        return result

    def process_many(self, listings: Iterable[Listing], llm_budget: int | None = None, notify: bool = True,
                     purpose: str = "pipeline") -> list[Result]:
        """Rules first for everything; the LLM (Claude or the team) only for the most promising `llm_budget` listings."""
        listings = list(listings)
        has_llm = bool(self.screener or self.team)
        if llm_budget is None or not has_llm:
            return [self.process(l, use_llm=True, notify=notify, purpose=purpose) for l in listings]
        prelim = [self.process(l, use_llm=False, notify=False) for l in listings]
        if self.team:
            from .agents import gate
            ranked = sorted(prelim, key=lambda r: gate.priority(r.listing, r.score, r.screen.catalog_key), reverse=True)
        else:
            ranked = sorted(prelim, key=_priority, reverse=True)
        chosen = {r.listing.key for r in ranked[:llm_budget] if not r.score.rejected and r.model_used == "rules"}
        out = []
        for r in prelim:
            if r.listing.key in chosen:
                self.db.conn.execute("DELETE FROM scores WHERE key=?", (r.listing.key,))
                out.append(self.process(r.listing, use_llm=True, notify=notify, purpose=purpose))
            elif self.team and r.model_used.startswith("team"):
                out.append(self.process(r.listing, use_llm=True, notify=notify, purpose=purpose))   # cached: 0 calls
            else:
                if self.team and not r.score.rejected and r.model_used == "rules":
                    from .agents import gate
                    if gate.is_candidate(r.listing, r.score, self.team.budget.tier, self.econ, r.screen.catalog_key) \
                            and self.team.store.get_verdict(r.listing.key) is None:
                        self.team.mark_pending(r.listing, r.score, r.screen.catalog_key)   # drained next run
                out.append(r)
        return out

    def drain_pending(self) -> int:
        """Screen listings queued by an earlier run (run cap / llm_budget); promote stale ones to UNSCREENED."""
        if not self.team:
            return 0
        n = 0
        for key in self.team.store.pending(limit=50):
            if not self.team.budget.run_ok("pipeline") or self.team.disabled_reason:
                break
            listing = self.db.get_listing(key)
            if listing is None:
                continue
            self.db.conn.execute("DELETE FROM scores WHERE key=?", (key,))
            self.process(listing, use_llm=True, notify=True)
            n += 1
        for key in self.team.store.stale_pending(self.settings.agent_max_wait_min * 60):
            listing, cached = self.db.get_listing(key), self.db.get_score(key)
            if listing is None:
                continue
            verdict = Verdict(decision="UNSCREENED", confidence="low", degraded=True,
                              reasons=["screening queue timed out (rules only)"])
            self.team.store.put_verdict(key, "", verdict, 0.0)
            screen = cached[0] if cached else rules.screen(listing)
            score = scoring.score(listing, screen, self.econ, self.model)
            score.alert = self.team.should_push(score, verdict)
            self.db.save_score(key, screen, score, "rules")
            result = Result(listing, screen, score, "rules", False, verdict)
            if score.alert and self.notifier and self.db.status(key) == "new":
                try:
                    self.notifier(result)
                    self.db.set_status(key, "notified")
                except Exception:
                    log.exception("notify failed for %s", key)
            n += 1
        return n

    # -- query-driven hunt -------------------------------------------------
    def hunt(self, raw_query: str, ebay=None, llm_budget: int = 15, notify: bool = False, live=None,
             purpose: str = "interactive") -> dict:
        hq = q.parse(raw_query)
        listings: list[Listing] = []
        errors: list[str] = []
        s = self.settings
        live_info: list[dict] = []

        if live is not None:
            from .sources.kleinanzeigen_live import LiveSearchBlocked
            for phrase in hq.variants[: s.ondemand_pages]:
                url = q.kleinanzeigen_url(phrase, hq, s.location_slug, s.location_id, s.radius_km)
                try:
                    fetched = live.search(url)
                except LiveSearchBlocked as exc:
                    errors.append(f"Kleinanzeigen live: {exc}")
                    break
                live_info.append({"url": fetched.url, "count": len(fetched.listings), "html": fetched.html})
                for l in fetched.listings:
                    # The search store is filled by upsert in process(); FTS then finds it next time too.
                    listings.append(l)

        if ebay is not None:
            seen = set()
            for phrase in hq.variants[:3]:
                try:
                    found = ebay.search(q.HuntQuery(raw=phrase, keywords=phrase.split(), max_price=hq.max_price,
                                                    min_price=hq.min_price), parts_only=True)
                except Exception as exc:  # network/auth problems shouldn't kill the whole hunt
                    errors.append(f"eBay: {exc}")
                    break
                for l in found:
                    if l.key not in seen:
                        seen.add(l.key)
                        listings.append(l)

        local = self.db.search(q.fts_query(hq), max_price=hq.max_price)
        known = {l.key for l in listings}
        local = [l for l in local if l.key not in known]
        listings += local
        if hq.shipping_only:
            listings = [l for l in listings if l.source == "ebay" or l.shipping]

        results = self.process_many(listings, llm_budget=llm_budget, notify=notify, purpose=purpose)
        results = [r for r in results if not r.score.rejected] + [r for r in results if r.score.rejected]
        results.sort(key=lambda r: (not r.score.rejected, r.score.ev), reverse=True)

        return {
            "query": hq,
            "results": results,
            "errors": errors,
            "live": live_info,
            "kleinanzeigen_links": [
                {"phrase": v, "url": q.kleinanzeigen_url(v, hq, s.location_slug, s.location_id, s.radius_km)}
                for v in hq.variants
            ] + [{"phrase": f"{hq.variants[0]} (ganz Deutschland, Versand)", "url": q.kleinanzeigen_url(hq.variants[0], hq)}],
            "ebay_sold_url": q.ebay_web_sold_url(hq.text.replace("defekt", "").strip()),
        }


def _priority(r: Result) -> float:
    # Unknown model (EV 0) still deserves a look if it's cheap: the LLM may identify it.
    if r.screen.catalog_key == "none":
        return 10.0 if (r.listing.price or 999) <= 120 else -1.0
    return r.score.ev
