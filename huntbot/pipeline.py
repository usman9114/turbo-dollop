"""Ingest → dedup → screen (rules, then Claude) → score → store → notify."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from . import query as q
from . import rules, scoring
from .config import Settings
from .db import DB
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
            "offer": draft_offer(self.listing, self.screen, self.score),
        }


class Pipeline:
    def __init__(self, settings: Settings, db: DB, screener: Screener | None = None,
                 notifier: Callable[[Result], None] | None = None):
        self.settings = settings
        self.db = db
        self.econ = settings.economics
        self.screener = screener
        if self.screener is None and settings.use_llm:
            self.screener = Screener(settings.screen_model, settings.review_model)
        self.notifier = notifier

    # -- scoring ----------------------------------------------------------
    def _screen(self, listing: Listing, use_llm: bool) -> tuple[Screen, Score, str]:
        rule = rules.screen(listing)
        best, model_used = rule, "rules"
        result = scoring.score(listing, rule, self.econ)
        if not (use_llm and self.screener) or result.rejected:
            return best, result, model_used

        first = self.screener.screen(listing)
        if first:
            best, model_used = rules.merge(rule, first), self.screener.screen_model
            result = scoring.score(listing, best, self.econ)
            escalate = (result.ev >= self.econ.min_ev) or (first.confidence == "low" and result.ev > 0)
            if escalate and not result.rejected:
                second = self.screener.review(listing)
                if second:
                    best, model_used = rules.merge(rule, second), self.screener.review_model
                    result = scoring.score(listing, best, self.econ)
        return best, result, model_used

    def process(self, listing: Listing, use_llm: bool = True, notify: bool = True) -> Result:
        listing.description = strip_pii(listing.description)
        is_new, changed = self.db.upsert_listing(listing, text_fingerprint(listing.title, listing.description))
        stored = self.db.get_listing(listing.key) or listing
        cached = self.db.get_score(listing.key)
        if cached and not changed:
            screen, _, model_used = cached
            return Result(stored, screen, scoring.score(stored, screen, self.econ), model_used, is_new)

        screen, score, model_used = self._screen(stored, use_llm)
        self.db.save_score(listing.key, screen, score, model_used)
        result = Result(stored, screen, score, model_used, is_new)

        if notify and score.alert and self.notifier and self.db.status(listing.key) == "new":
            try:
                self.notifier(result)
                self.db.set_status(listing.key, "notified")
            except Exception:  # notification failure must not lose the listing
                log.exception("notify failed for %s", listing.key)
        return result

    def process_many(self, listings: Iterable[Listing], llm_budget: int | None = None, notify: bool = True) -> list[Result]:
        """Rules first for everything; Claude only for the most promising `llm_budget` listings."""
        listings = list(listings)
        if llm_budget is None or not self.screener:
            return [self.process(l, use_llm=True, notify=notify) for l in listings]
        prelim = [self.process(l, use_llm=False, notify=False) for l in listings]
        ranked = sorted(prelim, key=_priority, reverse=True)
        chosen = {r.listing.key for r in ranked[:llm_budget] if not r.score.rejected and r.model_used == "rules"}
        out = []
        for r in prelim:
            if r.listing.key in chosen:
                self.db.conn.execute("DELETE FROM scores WHERE key=?", (r.listing.key,))
                out.append(self.process(r.listing, use_llm=True, notify=notify))
            else:
                out.append(r)
        return out

    # -- query-driven hunt -------------------------------------------------
    def hunt(self, raw_query: str, ebay=None, llm_budget: int = 15, notify: bool = False, live=None) -> dict:
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

        results = self.process_many(listings, llm_budget=llm_budget, notify=notify)
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
