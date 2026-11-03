"""Ties the pieces together: query -> sources -> screen -> score -> (details, deep review) -> ranked hits."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from hunter import kleinanzeigen as ka
from hunter.assessment import Assessment, Review, rules_assessment
from hunter.config import Settings
from hunter.models import Listing
from hunter.prices import PriceTable
from hunter.scoring import Score, score
from hunter.store import Store

GENERIC_QUERY = "business laptops (ThinkPad, Latitude, EliteBook) or Dyson stick vacuums to repair and resell"


@dataclass
class Plan:
    summary: str
    kleinanzeigen_queries: list[str]
    ebay_query: str
    category: str | None = None
    max_price: float | None = None
    exclude_words: list[str] = field(default_factory=list)


@dataclass
class Hit:
    listing: Listing
    assessment: Assessment
    score: Score
    screened_by: str  # model id, or "rules"


class Hunter:
    def __init__(self, settings: Settings, store: Store, prices: PriceTable, log=print):
        self.s = settings
        self.store = store
        self.prices = prices
        self.log = log
        self.claude_errors = 0

    # -- planning ---------------------------------------------------------
    def plan(self, query: str, max_price: float | None = None) -> Plan:
        if self.s.use_claude:
            try:
                from hunter.screen import plan_query

                p = plan_query(query, self.s.screen_model)
                return Plan(p.summary, p.kleinanzeigen_queries[:4] or [query], p.ebay_query or query,
                            p.category if p.category != "any" else None,
                            max_price if max_price is not None else p.max_price, p.exclude_words)
            except Exception as e:  # noqa: BLE001 - planning is optional
                self.log(f"  (query planning failed, searching literally: {e})")
        return Plan(query, [query], query, None, max_price, [])

    # -- assessment -------------------------------------------------------
    def assess(self, listing: Listing, query: str) -> tuple[Assessment, str]:
        if not self.s.use_claude:
            return rules_assessment(listing, self.prices, self.s.p_ok_cap), "rules"
        model = self.s.screen_model + (":detail" if listing.detailed else "")
        cached = self.store.cached_screening(listing.key, model, listing.price_eur)
        if cached:
            return Assessment.model_validate(cached).clamp(self.s.p_ok_cap), self.s.screen_model
        try:
            from hunter.screen import screen

            a = screen(listing, query, self.prices, self.s.screen_model).clamp(self.s.p_ok_cap)
            return a, self.s.screen_model
        except Exception as e:  # noqa: BLE001 - fall back to rules, keep hunting
            self.claude_errors += 1
            if self.claude_errors <= 3:
                self.log(f"  (Claude screening failed for {listing.key}, using rules: {e})")
            return rules_assessment(listing, self.prices, self.s.p_ok_cap), "rules"

    def _evaluate_one(self, listing: Listing, query: str) -> Hit:
        a, by = self.assess(listing, query)
        sc = score(listing, a, self.prices, self.s)
        key_model = by + (":detail" if listing.detailed and by != "rules" else "")
        self.store.save_screening(listing.key, key_model, a.model_dump(), sc.__dict__, sc.ev, sc.alert)
        return Hit(listing, a, sc, by)

    # -- main entry -------------------------------------------------------
    def evaluate(self, listings: list[Listing], query: str, *, plan: Plan | None = None,
                 ka_client: ka.KleinanzeigenClient | None = None, details: int = 0,
                 deep: int = 3) -> list[Hit]:
        plan = plan or Plan(query, [query], query)
        excl = [w.lower() for w in plan.exclude_words if w.strip()]
        kept: list[Listing] = []
        for l in drop_reposts(listings):
            title = l.title.lower()
            if excl and any(w in title for w in excl):
                continue
            if plan.max_price is not None and l.price_eur is not None and l.price_eur > plan.max_price:
                continue
            if l.distance_km is not None and l.distance_km > self.s.radius_km and not l.shipping_possible:
                continue
            self.store.upsert_listing(l)
            kept.append(l)

        self.log(f"Screening {len(kept)} listings ({'Claude ' + self.s.screen_model if self.s.use_claude else 'keyword rules'})…")
        hits = [self._evaluate_one(l, query) for l in kept]
        hits.sort(key=lambda h: h.score.rank_value, reverse=True)

        # Tier 1: read the full ad page for the best few (counts against the rate limit).
        if ka_client is not None and details > 0:
            todo = [h for h in hits if h.listing.source == "kleinanzeigen" and not h.listing.detailed
                    and not h.score.rejected][:details]
            for h in todo:
                self.log(f"  reading ad page: {h.listing.title[:60]}")
                try:
                    full = ka_client.ad(h.listing.url)
                except ka.Blocked as e:
                    self.log(f"  stopped reading ad pages: {e}")
                    break
                full.distance_km = h.listing.distance_km
                if not full.image_urls:
                    full.image_urls = h.listing.image_urls
                self.store.upsert_listing(full)
                new = self._evaluate_one(full, query)
                hits[hits.index(h)] = new
            hits.sort(key=lambda h: h.score.rank_value, reverse=True)

        # Deep review with the stronger model for the promising ones.
        if self.s.use_claude and deep > 0:
            todo = [h for h in hits if not h.score.rejected and (
                (h.score.ev or -1e9) >= self.s.deep_review_ev
                or (h.assessment.confidence < 0.5 and (h.score.ev or -1e9) >= self.s.deep_review_ev / 2)
            )][:deep]
            for h in todo:
                self.log(f"  deep review ({self.s.review_model}): {h.listing.title[:60]}")
                try:
                    from hunter.screen import deep_review

                    r = deep_review(h.listing, query, self.prices, self.s.review_model, h.score.max_buy)
                    r.clamp(self.s.p_ok_cap)
                except Exception as e:  # noqa: BLE001
                    self.log(f"  (deep review failed: {e})")
                    continue
                sc = score(h.listing, r, self.prices, self.s)
                self.store.save_screening(h.listing.key, self.s.review_model, r.model_dump(), sc.__dict__, sc.ev, sc.alert)
                hits[hits.index(h)] = Hit(h.listing, r, sc, self.s.review_model)
            hits.sort(key=lambda h: h.score.rank_value, reverse=True)
        return hits

    def notify(self, hits: list[Hit], only_new: bool = True) -> int:
        if not self.s.telegram_enabled:
            return 0
        from hunter.telegram import send_card

        sent = 0
        for h in hits:
            if not h.score.alert or (only_new and self.store.was_notified(h.listing.key)):
                continue
            try:
                send_card(self.s.telegram_token, self.s.telegram_chat_id, h)
                self.store.mark_notified(h.listing.key)
                sent += 1
                time.sleep(1)
            except Exception as e:  # noqa: BLE001
                self.log(f"  (Telegram send failed: {e})")
        return sent


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", title.lower()).strip()


def drop_reposts(listings: list[Listing]) -> list[Listing]:
    """Same seller re-posting the same item: same price + postcode and a near-identical title."""
    kept: list[Listing] = []
    for l in listings:
        dup = any(
            k.source == l.source and k.price_eur == l.price_eur and k.postal_code == l.postal_code
            and SequenceMatcher(None, _norm(k.title), _norm(l.title)).ratio() > 0.9
            for k in kept
        )
        if not dup:
            kept.append(l)
    return kept


def is_review(a: Assessment) -> bool:
    return isinstance(a, Review)
