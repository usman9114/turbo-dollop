"""Gatekeeper: LLM calls go only to listings that could plausibly be a deal."""

from __future__ import annotations

from typing import Literal

from ..config import Economics
from ..models import Listing, Score


def threshold(score: Score, econ: Economics) -> float:
    return econ.flip_min_ev if score.mode == "flip" else econ.min_ev


def _far_no_ship(score: Score) -> bool:
    return any("outside pickup area and no shipping" in r for r in score.reasons)


def is_candidate(listing: Listing, score: Score, tier: Literal["strict", "lenient"], econ: Economics | None = None,
                 catalog_key: str = "none") -> bool:
    if score.rejected or _far_no_ship(score):
        return False
    econ = econ or Economics()
    strict = (score.alert
              or any(r.startswith("CHECK: >70% under typical") for r in score.reasons)
              or (catalog_key == "none" and listing.price is not None and listing.price <= 120
                  and len(listing.description) >= 40))
    if strict or tier == "strict":
        return strict
    return (score.ev >= 0.5 * threshold(score, econ)
            or (score.mode == "flip" and score.discount is not None and score.discount >= 0.15)
            or (listing.price is None and score.fair_ask is not None))


def still_candidate(score: Score, econ: Economics | None = None) -> bool:
    econ = econ or Economics()
    if score.rejected:
        return False
    return (score.alert or (score.mode == "flip" and score.discount is not None and score.discount >= 0.15)
            or score.ev >= 0.5 * threshold(score, econ))


def priority(listing: Listing, score: Score, catalog_key: str = "none") -> float:
    if catalog_key == "none":
        base = 10.0 if (listing.price if listing.price is not None else 999) <= 120 else -1.0
    else:
        base = score.ev
    if any(r.startswith("CHECK: >70% under typical") for r in score.reasons):
        base += 50
    if score.alert:
        base += 30
    return base
