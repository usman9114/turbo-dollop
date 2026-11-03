"""Offer drafts (German). The bot never sends them: you copy, check, and send yourself."""

from __future__ import annotations

import math

from .models import Listing, Score, Screen

COPY_TEXT_LIMIT = 256  # Telegram copy_text button limit

DEFAULT_QUESTIONS = [
    "Ist ein BIOS-Passwort gesetzt?",
    "Ist es von Microsoft-/Firmenkonto abgemeldet?",
    "Foto vom Typenschild mit Seriennummer?",
]


def offer_price(listing: Listing, score: Score) -> int:
    """Anchor below your ceiling: 85% of max_buy, never above the (VB-adjusted) asking price."""
    if score.max_buy <= 0:
        return 0
    target = score.max_buy * 0.85
    if listing.price:
        target = min(target, score.buy_estimate or listing.price)
    return max(5, int(math.floor(target / 5) * 5))


def draft_offer(listing: Listing, screen: Screen, score: Score) -> str:
    price = offer_price(listing, score)
    if price <= 0:
        return ""
    questions = (screen.questions_for_seller or [])[:1] + DEFAULT_QUESTIONS
    seen, qs = set(), []
    for q in questions:
        if q.lower() not in seen:
            seen.add(q.lower())
            qs.append(q)
    name = screen.device_model if screen.device_model != "unknown" else listing.title
    head = f"Hallo, ich interessiere mich für {name[:40]}. Wären {price} € ok? "
    tail = " Danke & Grüße"
    body = ""
    for q in qs:
        if len(head) + len(body) + len(q) + 1 + len(tail) > COPY_TEXT_LIMIT:
            break
        body += q + " "
    return (head + body.rstrip() + tail)[:COPY_TEXT_LIMIT]
