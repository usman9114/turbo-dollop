"""Tier 0 input: Kleinanzeigen's own saved-search alert emails, read over IMAP.

Set up 10-15 saved searches in the Kleinanzeigen app/website with email alerts
going to a dedicated mailbox (e.g. a new Gmail account with an app password),
then point HUNTER_IMAP_* at it. No request goes to Kleinanzeigen for this.

The email layout isn't documented, so the parser is deliberately generic: it pulls
every link that contains an ad ID, plus the title and price shown next to it.
Collect a week of real alerts and run `python -m hunter alerts --dry-run` to check it.
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta
from email.message import Message
from urllib.parse import unquote

from bs4 import BeautifulSoup

from hunter.kleinanzeigen import BASE, ad_id_from_url, parse_price
from hunter.models import Listing

_PRICE_IN_TEXT = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,\d{2})?\s*€(?:\s*VB)?|\bVB\b|Zu verschenken")
_PLZ_IN_TEXT = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][\wäöüß.\- ]{1,40})")


def _html_body(msg: Message) -> str | None:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return None


def parse_alert_html(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Listing] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        ad_id = ad_id_from_url(href) or ad_id_from_url(unquote(unquote(href)))
        if not ad_id:
            continue
        listing = found.get(ad_id)
        if listing is None:
            clean = unquote(unquote(href))
            m = re.search(r"/s-anzeige/[^?#\s\"']+", clean)
            url = BASE + m.group(0) if m else f"{BASE}/s-anzeige/{ad_id}"
            listing = found[ad_id] = Listing(source="kleinanzeigen", ad_id=ad_id, url=url, title="")
        text = a.get_text(" ", strip=True)
        if len(text) > len(listing.title) and not _PRICE_IN_TEXT.fullmatch(text):
            listing.title = text
        img = a.find("img")
        if img and img.get("src", "").startswith("http") and not listing.image_urls:
            listing.image_urls = [img["src"]]

        # Price / place: look in the smallest enclosing block that mentions this ad only.
        block = a
        for _ in range(6):
            block = block.parent
            if block is None:
                break
            ids = {ad_id_from_url(unquote(x["href"])) for x in block.find_all("a", href=True)} - {None}
            if len(ids) > 1:
                break
            block_text = block.get_text(" ", strip=True)
            if listing.price_eur is None and not listing.free and (pm := _PRICE_IN_TEXT.search(block_text)):
                if (p := parse_price(pm.group(0))) is not None:
                    listing.price_eur, listing.negotiable, listing.free = p
                    listing.price_text = pm.group(0)
            if not listing.postal_code and (lm := _PLZ_IN_TEXT.search(block_text)):
                listing.postal_code, listing.location = lm.group(1), lm.group(0).strip()
    return [x for x in found.values() if x.title]


def fetch_alerts(host: str, user: str, password: str, folder: str = "INBOX",
                 days: int = 2, sender: str = "kleinanzeigen") -> list[Listing]:
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    out: dict[str, Listing] = {}
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder, readonly=True)
        status, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
        if status != "OK":
            return []
        for num in data[0].split():
            status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            html = _html_body(email.message_from_bytes(msg_data[0][1]))
            if html:
                for listing in parse_alert_html(html):
                    out.setdefault(listing.ad_id, listing)
    return list(out.values())
