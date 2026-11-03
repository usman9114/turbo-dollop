"""Kleinanzeigen input WITHOUT automated requests to kleinanzeigen.de (ToS §5):

1. Saved-search alert emails you already receive (IMAP).
2. Listings the browser userscript reads from pages *you* opened.

Both are parsed into `Listing` objects here.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from email.message import EmailMessage
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..models import Listing
from ..privacy import postcode_only, seller_hash, strip_pii

log = logging.getLogger(__name__)

AD_URL = re.compile(r"https?://(?:www\.)?kleinanzeigen\.de/s-anzeige/([a-z0-9\-]+)/(\d{6,})(?:-[\d-]+)?", re.I)
PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))?\s*€")
POSTCODE = re.compile(r"\b(\d{5})\b(?:\s+([A-ZÄÖÜ][\wäöüß\-]+(?:\s[A-ZÄÖÜ][\wäöüß\-]+)?))?")
SENDER_HINT = re.compile(r"kleinanzeigen", re.I)


def parse_price(text: str) -> tuple[float | None, bool]:
    """'150 € VB' -> (150.0, True); 'Zu verschenken' -> (0.0, False); 'VB' -> (None, True)."""
    t = text.strip()
    negotiable = bool(re.search(r"\bVB\b|verhandlungsbasis", t, re.I))
    if re.search(r"zu verschenken|gratis|kostenlos", t, re.I):
        return 0.0, False
    m = PRICE.search(t)
    if not m:
        return None, negotiable
    whole = m.group(1).replace(".", "")
    return float(f"{whole}.{m.group(2) or '00'}"), negotiable


def canonical_url(slug: str, ad_id: str) -> str:
    return f"https://www.kleinanzeigen.de/s-anzeige/{slug}/{ad_id}"


# ---------------------------------------------------------------------------
# 1. Alert emails
# ---------------------------------------------------------------------------

def _find_ad_links(soup: BeautifulSoup):
    """Yield (anchor, slug, ad_id). Handles tracking redirects that wrap the real URL."""
    for a in soup.find_all("a", href=True):
        href = unquote(unquote(a["href"]))
        m = AD_URL.search(href)
        if m:
            yield a, m.group(1), m.group(2)


def _block_for(anchor) -> object:
    """Smallest ancestor that contains a price (each ad in the mail is one table cell/row)."""
    node = anchor
    for _ in range(8):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        if "€" in text or re.search(r"zu verschenken|\bVB\b", text, re.I):
            if len(text) < 600:
                return node
            break
    return anchor.parent or anchor


def parse_alert_html(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Listing] = {}
    slugged: dict[str, bool] = {}
    for anchor, slug, ad_id in _find_ad_links(soup):
        block = _block_for(anchor)
        block_text = block.get_text(" ", strip=True)
        title = anchor.get_text(" ", strip=True)
        from_slug = False
        if not title or len(title) < 4 or title.lower() in {"ansehen", "anzeige ansehen", "zur anzeige"}:
            title = (anchor.get("title") or "").strip()
            if not title:
                title, from_slug = slug.replace("-", " "), True
        price, vb = parse_price(block_text)
        img = block.find("img", src=True) if hasattr(block, "find") else None
        pc = POSTCODE.search(block_text)
        existing = found.get(ad_id)
        listing = Listing(
            source="kleinanzeigen",
            ad_id=ad_id,
            title=title[:200],
            url=canonical_url(slug, ad_id),
            price=price,
            negotiable=vb,
            description="",
            location=" ".join(filter(None, pc.groups())) if pc else "",
            shipping=True if re.search(r"versand möglich", block_text, re.I) else None,
            image_urls=[img["src"]] if img and img["src"].startswith("http") else [],
            raw_source="email",
        )
        # Several anchors per ad (image + title): merge, preferring real link text over the URL slug.
        if existing is None:
            found[ad_id] = listing
            slugged[ad_id] = from_slug
            continue
        if slugged[ad_id] and not from_slug:
            existing.title, slugged[ad_id] = listing.title, False
        existing.price = existing.price if existing.price is not None else listing.price
        existing.negotiable = existing.negotiable or listing.negotiable
        existing.image_urls = existing.image_urls or listing.image_urls
        existing.location = existing.location or listing.location
    return list(found.values())


def parse_alert_email(raw: bytes) -> list[Listing]:
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        return []
    content = body.get_content()
    if body.get_content_type() == "text/plain":
        content = "".join(f'<p><a href="{u}">{u}</a></p>' for u in re.findall(r"https?://\S+", content))
    return parse_alert_html(content)


def fetch_alert_emails(host: str, user: str, password: str, folder: str = "INBOX", mark_seen: bool = True) -> list[Listing]:
    """Read UNSEEN Kleinanzeigen alert mails over IMAP (your own mailbox, not the platform)."""
    listings: list[Listing] = []
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder)
        status, data = imap.search(None, "UNSEEN", "FROM", '"kleinanzeigen"')
        if status != "OK":
            return []
        for num in data[0].split():
            status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            parsed = parse_alert_email(msg_data[0][1])
            log.info("alert mail %s: %d listings", num.decode(), len(parsed))
            listings.extend(parsed)
            if mark_seen:
                imap.store(num, "+FLAGS", "\\Seen")
    return listings


# ---------------------------------------------------------------------------
# 2. Browser companion (userscript) payloads
# ---------------------------------------------------------------------------

def from_companion(item: dict) -> Listing | None:
    """Validate one item posted by userscript/huntbot.user.js."""
    url = str(item.get("url", ""))
    m = AD_URL.search(url)
    ad_id = str(item.get("ad_id") or (m.group(2) if m else "")).strip()
    if not ad_id.isdigit():
        return None
    price, vb = parse_price(str(item.get("price_text", "")))
    images = [u for u in item.get("images", []) if isinstance(u, str) and u.startswith("https://")][:10]
    shipping_text = str(item.get("shipping_text", ""))
    return Listing(
        source="kleinanzeigen",
        ad_id=ad_id,
        title=str(item.get("title", "")).strip()[:200],
        url=canonical_url(m.group(1), ad_id) if m else url,
        price=price,
        negotiable=vb,
        description=strip_pii(str(item.get("description", "")))[:5000],
        location=postcode_only(str(item.get("location", "")))[:80],
        shipping=True if re.search(r"versand", shipping_text, re.I) else (False if shipping_text else None),
        image_urls=images,
        seller_hash=seller_hash(str(item.get("seller", ""))),
        raw_source="companion",
    )
