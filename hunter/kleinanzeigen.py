"""Kleinanzeigen: HTML parsers plus an opt-in, rate-limited Playwright fetcher.

Read this before turning the fetcher on (plan §2 #1 and §4.1):
  * Kleinanzeigen's terms (§5) forbid automated access, including read-only crawlers,
    and the site IP-blocks them. Using the fetcher is your decision and your risk.
  * The fetcher is therefore OFF unless HUNTER_KA_LIVE=1, never logs in, never sends
    messages, never rotates proxies, never solves CAPTCHAs and never runs on a schedule.
    It only runs when you type a search, at most one page per HUNTER_KA_MIN_INTERVAL_S
    (default 30 s) and HUNTER_KA_DAILY_CAP pages per day (default 100).
  * Any 403/429 or CAPTCHA pauses it for HUNTER_KA_BLOCK_HOURS (default 24 h).
The parsers also work on pages you saved yourself and on the alert emails
(see email_alerts.py), which need no automated access at all.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from hunter.models import Listing

BASE = "https://www.kleinanzeigen.de"
HOST = "www.kleinanzeigen.de"
CATEGORIES = {"notebooks": 278}  # extend as needed (category id is in each ad URL)

_PRICE_RE = re.compile(r"^(?:(?P<eur>\d{1,3}(?:\.\d{3})+|\d+)(?:,(?P<ct>\d{1,2}))?\s*€)?\s*(?P<vb>VB)?$")
_PLZ_RE = re.compile(r"^(?P<plz>\d{5})\s+(?P<place>.+)$")
_DIST_RE = re.compile(r"^\((?P<km>\d+(?:[.,]\d+)?)\s*km\)$")
_DATE_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4}|Heute,?\s*\d{1,2}:\d{2}|Gestern,?\s*\d{1,2}:\d{2})$")
_AD_ID_RE = re.compile(r"/s-anzeige/[^/?#]+/(\d{6,})")
_SOLD_IN_TITLE = re.compile(r"(?<!wird )\b(verkauft|reserviert)\b(?!\s+wird)", re.IGNORECASE)


def status_from_title(title: str) -> str:
    """Sellers often edit '*VERKAUFT*' or 'RESERVIERT' into the title instead of deleting the ad."""
    m = _SOLD_IN_TITLE.search(title)
    if not m:
        return "active"
    return "sold" if m.group(1).lower() == "verkauft" else "reserved"


class Blocked(RuntimeError):
    """Kleinanzeigen refused us (403/429/CAPTCHA) or a local limit was hit."""


def parse_price(text: str) -> tuple[float | None, bool, bool] | None:
    """'60 € VB' -> (60.0, True, False); 'Zu verschenken' -> (0.0, False, True); else None."""
    t = text.replace("\xa0", " ").strip()
    if t.lower() == "zu verschenken":
        return 0.0, False, True
    m = _PRICE_RE.match(t)
    if not m or not (m.group("eur") or m.group("vb")):
        return None
    eur = None
    if m.group("eur"):
        eur = float(m.group("eur").replace(".", "")) + (float(f"0.{m.group('ct')}") if m.group("ct") else 0.0)
    return eur, bool(m.group("vb")), False


def ad_id_from_url(url: str) -> str | None:
    m = _AD_ID_RE.search(url)
    return m.group(1) if m else None


def search_url(keywords: str, postal_code: str | None = None, radius_km: int | None = None,
               min_price: float | None = None, max_price: float | None = None,
               category: str | int | None = None) -> str:
    cat = CATEGORIES.get(str(category), category) if category else ""
    params = {
        "keywords": keywords,
        "categoryId": cat or "",
        "locationStr": postal_code or "",
        "radius": radius_km or "",
        "minPrice": int(min_price) if min_price is not None else "",
        "maxPrice": int(max_price) if max_price is not None else "",
        "sortingField": "SORTING_DATE",
        "action": "find",
    }
    return f"{BASE}/s-suchanfrage.html?{urlencode(params)}"


def page_url(first_page_url: str, page: int) -> str:
    """'/s-80331/thinkpad-defekt/k0l6443r50' -> '/s-80331/seite:2/thinkpad-defekt/k0l6443r50'."""
    if page <= 1:
        return first_page_url
    parts = urlsplit(first_page_url)
    segs = [s for s in parts.path.split("/") if s and not s.startswith("seite:")]
    if len(segs) < 2:
        return first_page_url
    segs.insert(len(segs) - 2, f"seite:{page}")
    return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(segs), parts.query, ""))


def _big_image(url: str) -> str:
    return re.sub(r"\?rule=\$_\d+\.AUTO$", "?rule=$_59.AUTO", url)


def parse_search(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    for art in soup.select("article[data-adid]"):
        ad_id = art.get("data-adid", "").strip()
        href = art.get("data-href") or ""
        if not href:
            a = art.select_one('a[href*="/s-anzeige/"]')
            href = a["href"] if a else ""
        if not ad_id or not href:
            continue

        ld: dict = {}
        for sc in art.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(sc.string or "{}")
                break
            except json.JSONDecodeError:
                continue
        img = ld.get("contentUrl")
        if not img:
            tag = art.select_one("img[src], img[data-imgsrc]")
            img = (tag.get("data-imgsrc") or tag.get("src")) if tag else None

        heading = art.select_one("h2, h3")
        title = (ld.get("title") or (heading.get_text(" ", strip=True) if heading else "")).strip()
        description = (ld.get("description") or "").strip()

        for junk in art.select("script, svg, style"):
            junk.extract()
        strings = [s.strip() for s in art.stripped_strings if s.strip()]

        listing = Listing(source="kleinanzeigen", ad_id=ad_id, url=urljoin(BASE, href),
                          title=title, description=description, status=status_from_title(title),
                          image_urls=[_big_image(img)] if img else [])
        price_seen = False
        for i, s in enumerate(strings):
            if s in (title, description) or description.startswith(s.rstrip(".…")):
                continue
            if i == 0 and s.isdigit():
                listing.photo_count = int(s)
                continue
            if (m := _PLZ_RE.match(s)) and not listing.postal_code:
                listing.postal_code, listing.location = m.group("plz"), s
            elif (m := _DIST_RE.match(s)) and listing.distance_km is None:
                listing.distance_km = float(m.group("km").replace(",", "."))
            elif _DATE_RE.match(s) and not listing.posted:
                listing.posted = s
            elif not price_seen and (p := parse_price(s)) is not None:
                listing.price_eur, listing.negotiable, listing.free = p
                listing.price_text = s
                price_seen = True
            elif s == "Versand möglich":
                listing.shipping_possible = True
            elif s == "Direkt kaufen":
                listing.direct_buy = True
                listing.shipping_possible = True
        out.append(listing)
    return out


def parse_ad(html: str, url: str) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    ad_id = ad_id_from_url(url) or ""
    id_box = soup.select_one("#viewad-ad-id-box")
    if id_box and (m := re.search(r"\d{6,}", id_box.get_text())):
        ad_id = m.group(0)

    status = "active"
    title_el = soup.select_one("#viewad-title")
    title = ""
    if title_el:
        for badge in title_el.select("span"):
            classes = badge.get("class") or []
            if "is-hidden" not in classes:
                label = badge.get_text(strip=True).lower()
                status = "reserved" if "reserviert" in label else "deleted" if "gelöscht" in label else status
            badge.extract()
        title = title_el.get_text(" ", strip=True)
        if title_el.get("data-soldlabel") and "is-sold" in (title_el.get("class") or []):
            status = "sold"
        if status == "active":
            status = status_from_title(title)

    listing = Listing(source="kleinanzeigen", ad_id=ad_id, url=url, title=title, status=status, detailed=True)

    if (el := soup.select_one("#viewad-price")) and (p := parse_price(el.get_text(" ", strip=True))):
        listing.price_eur, listing.negotiable, listing.free = p
        listing.price_text = el.get_text(" ", strip=True)
    if el := soup.select_one("#viewad-locality"):
        listing.location = el.get_text(" ", strip=True)
        if m := re.match(r"(\d{5})", listing.location):
            listing.postal_code = m.group(1)
    if el := soup.select_one("#viewad-extra-info span"):
        listing.posted = el.get_text(strip=True)
    if el := soup.select_one("#viewad-description-text"):
        for br in el.find_all("br"):
            br.replace_with("\n")
        listing.description = el.get_text().strip()

    for li in soup.select("li.addetailslist--detail"):
        value = li.select_one(".addetailslist--detail--value")
        if value is None:
            continue
        v = value.get_text(" ", strip=True)
        value.extract()
        listing.attributes[li.get_text(" ", strip=True)] = v

    images = []
    for img in soup.select(".galleryimage-element img, #viewad-image"):
        src = img.get("data-imgsrc") or img.get("src")
        if src and src.startswith("http") and src not in images:
            images.append(_big_image(src))
    listing.image_urls = images
    listing.photo_count = len(images) or None

    contact = soup.select_one("#viewad-contact")
    contact_text = contact.get_text(" ", strip=True) if contact else ""
    if "Gewerblicher" in contact_text or "Gewerblich" in contact_text:
        listing.seller_type = "commercial"
    elif "Privat" in contact_text:
        listing.seller_type = "private"
    text = soup.get_text(" ", strip=True)
    listing.shipping_possible = "Versand möglich" in text or "Direkt kaufen" in text
    listing.direct_buy = "Direkt kaufen" in text
    return listing


def looks_blocked(html: str) -> bool:
    low = html.lower()
    return ("captcha" in low or "ip-eingeschraenkt" in low or "zugriff verweigert" in low) and \
        "data-adid" not in low and "viewad-title" not in low


class KleinanzeigenClient:
    """Opt-in Playwright fetcher. Use as a context manager."""

    def __init__(self, store, *, min_interval_s: float, daily_cap: int, block_hours: float,
                 chromium_path: str | None = None, headless: bool = True, log=print):
        self.store = store
        self.min_interval_s = max(5.0, min_interval_s)
        self.daily_cap = daily_cap
        self.block_hours = block_hours
        self.chromium_path = chromium_path
        self.headless = headless
        self.log = log
        self._pw = self._browser = self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {"headless": self.headless}
        if self.chromium_path:
            kwargs["executable_path"] = self.chromium_path
        self._browser = self._pw.chromium.launch(**kwargs)
        ctx = self._browser.new_context(locale="de-DE", timezone_id="Europe/Berlin")
        # Don't download images/fonts: fewer requests, and Claude reads images by URL anyway.
        ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image", "font", "media"} else r.continue_())
        self._page = ctx.new_page()
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _get(self, url: str) -> tuple[str, str]:
        if blocked := self.store.blocked_until(HOST):
            until, reason = blocked
            raise Blocked(f"paused until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))} ({reason})")
        if self.store.fetches_since(HOST, time.time() - 86400) >= self.daily_cap:
            raise Blocked(f"daily cap of {self.daily_cap} pages reached")
        last = self.store.last_fetch(HOST)
        if last is not None and (wait := last + self.min_interval_s - time.time()) > 0:
            self.log(f"  waiting {wait:.0f}s (rate limit)…")
            time.sleep(wait)

        self.store.log_fetch(HOST)
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        html = self._page.content()
        status = resp.status if resp else 0
        if status in (403, 429) or looks_blocked(html):
            reason = f"HTTP {status}" if status in (403, 429) else "CAPTCHA / IP block page"
            self.store.set_block(HOST, self.block_hours, reason)
            raise Blocked(f"Kleinanzeigen refused the request ({reason}); pausing {self.block_hours:.0f} h")
        if status >= 400:
            raise RuntimeError(f"HTTP {status} for {url}")
        return html, self._page.url

    def search(self, keywords: str, *, postal_code=None, radius_km=None, min_price=None,
               max_price=None, category=None, pages: int = 1) -> list[Listing]:
        first = search_url(keywords, postal_code, radius_km, min_price, max_price, category)
        results: list[Listing] = []
        seen: set[str] = set()
        final_first = first
        for page in range(1, pages + 1):
            url = first if page == 1 else page_url(final_first, page)
            html, final = self._get(url)
            if page == 1:
                final_first = final
            batch = [x for x in parse_search(html) if x.ad_id not in seen]
            if not batch:
                break
            seen.update(x.ad_id for x in batch)
            results += batch
        return results

    def ad(self, url: str) -> Listing:
        html, final = self._get(url)
        return parse_ad(html, final)
