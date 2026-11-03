"""Telegram deal cards. The bot only posts to YOUR chat; it never messages sellers.

Create a bot with @BotFather, send it any message, then get your chat id from
https://api.telegram.org/bot<TOKEN>/getUpdates (message.chat.id).
"""

from __future__ import annotations

import html

import httpx2 as httpx

API = "https://api.telegram.org/bot{token}/{method}"


def _esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def card_text(hit) -> str:
    l, a, sc = hit.listing, hit.assessment, hit.score
    lines = [f"<b>{_esc(l.title[:120])}</b>"]
    price = l.price_text or (f"{l.price_eur:.0f} €" if l.price_eur is not None else "no price")
    where = l.location or l.source
    if l.distance_km is not None:
        where += f" ({l.distance_km:.0f} km)"
    lines.append(f"{_esc(price)} · {_esc(where)} · {'pickup' if sc.pickup else 'shipped'}")
    if sc.ev is not None:
        lines.append(
            f"<b>EV €{sc.ev:.0f}</b> · €{sc.ev_per_hour or 0:.0f}/h · ROI {100 * (sc.roi or 0):.0f}% · "
            f"max buy €{sc.max_buy or 0:.0f}"
        )
    elif sc.max_buy is not None:
        lines.append(f"Pay at most <b>€{sc.max_buy:.0f}</b>")
    lines.append(f"🔧 {_esc(a.model_guess[:80])}: {_esc(a.likely_fix[:120])} (p_ok {a.p_ok:.0%})")
    if a.red_flags:
        lines.append("⚠️ " + _esc(", ".join(a.red_flags)))
    if sc.rejected:
        lines.append("⛔ " + _esc(sc.rejected))
    if a.notes:
        lines.append(f"<i>{_esc(a.notes[:300])}</i>")
    return "\n".join(lines)[:1024]  # photo caption limit


def send_card(token: str, chat_id: str, hit) -> None:
    buttons = [[{"text": "Open ad", "url": hit.listing.url}]]
    offer = getattr(hit.assessment, "offer_message_de", None)
    if offer:
        buttons[0].append({"text": "Copy offer", "copy_text": {"text": offer[:256]}})
    payload = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    text = card_text(hit)
    with httpx.Client(timeout=30) as http:
        if hit.listing.image_urls:
            r = http.post(API.format(token=token, method="sendPhoto"),
                          json={**payload, "photo": hit.listing.image_urls[0], "caption": text})
            if r.status_code == 200:
                _send_offer(http, token, chat_id, offer)
                return
        r = http.post(API.format(token=token, method="sendMessage"),
                      json={**payload, "text": text, "link_preview_options": {"is_disabled": True}})
        r.raise_for_status()
        _send_offer(http, token, chat_id, offer)


def _send_offer(http, token: str, chat_id: str, offer: str | None) -> None:
    # Copy buttons hold 256 chars; send the full draft too so nothing is cut off.
    if offer and len(offer) > 256:
        http.post(API.format(token=token, method="sendMessage"),
                  json={"chat_id": chat_id, "text": offer})
