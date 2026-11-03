"""On-demand Kleinanzeigen search ("Tier 1", opt-in).

⚠️ This sends automated requests to kleinanzeigen.de, which its terms (§5) forbid.
It is OFF by default (HUNTBOT_ONDEMAND=true to enable) and deliberately conservative:

- one results page per query you trigger by hand, never scheduled polling;
- at least 30 s between requests and at most 100 per day (persisted in the DB);
- an honest User-Agent that says it is a bot; no proxies, no CAPTCHA solving, no login;
- on HTTP 403/429, a CAPTCHA or the "IP eingeschränkt" page it stops for 24 hours.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx2 as httpx
from bs4 import BeautifulSoup

from ..db import DB
from ..models import Listing
from ..privacy import postcode_only, strip_pii
from .kleinanzeigen import AD_URL, canonical_url, parse_price

log = logging.getLogger(__name__)

USER_AGENT = "huntbot/0.1 (personal deal-finder bot; low rate; +https://github.com/usman9114/turbo-dollop)"
MIN_INTERVAL_S = 30
DAILY_CAP = 100
BACKOFF_S = 24 * 3600
BLOCK_HINTS = re.compile(r"captcha|ip-eingeschraenkt|ip eingeschränkt|zugriff verweigert|access denied", re.I)


class LiveSearchBlocked(RuntimeError):
    """Kleinanzeigen refused us, or our own limits say stop."""


@dataclass
class FetchResult:
    url: str
    status: int
    listings: list[Listing]
    html: str = ""


PRICE_TEXT = re.compile(r"^(\d[\d.]*(?:,\d{2})?\s*€(?:\s*VB)?|VB|Zu verschenken)$", re.I)
LOCATION_TEXT = re.compile(r"^\d{5}\s+\S")


def _own_texts(node) -> list[str]:
    """Text of every element, each counted once (its direct strings), in document order."""
    out = []
    for el in node.find_all(True):
        if el.name in ("script", "style", "svg"):
            continue
        own = " ".join(t.strip() for t in el.find_all(string=True, recursive=False) if t.strip())
        if own:
            out.append(re.sub(r"\s+", " ", own))
    return out


def _jsonld(art) -> dict:
    for tag in art.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def parse_search_html(html: str) -> list[Listing]:
    """Result list → listings. Layout-independent: embedded JSON-LD first, then text patterns,
    so Tailwind class changes on kleinanzeigen.de don't break it."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Listing] = {}
    for art in soup.select("article[data-adid], article.aditem"):
        href = art.get("data-href") or ""
        if not href:
            a = art.select_one("a[href*='/s-anzeige/']")
            href = a.get("href", "") if a else ""
        m = AD_URL.search("https://www.kleinanzeigen.de" + href if href.startswith("/") else href)
        ad_id = art.get("data-adid") or (m.group(2) if m else "")
        if not ad_id or not ad_id.isdigit():
            continue
        ld = _jsonld(art)
        texts = _own_texts(art)

        title = (ld.get("title") or "").strip()
        if not title:
            h = art.find(["h2", "h3"])
            title = h.get_text(" ", strip=True) if h else ""
        if not title:
            for a in art.select("a[href*='/s-anzeige/']"):
                t = a.get_text(" ", strip=True)
                if t and not a.find("img") and not t.isdigit():
                    title = t
                    break
        if not title:
            img = art.find("img", alt=True)
            title = (img["alt"].replace(" Vorschau", "") if img else "").strip()
        if not title and m:
            title = m.group(1).replace("-", " ")

        description = (ld.get("description") or "").strip()
        if not description:
            paras = [p.get_text(" ", strip=True) for p in art.find_all("p")]
            paras = [p for p in paras if len(p) > 25 and not PRICE_TEXT.match(p)]
            description = paras[0] if paras else ""

        price_text = next((t for t in texts if PRICE_TEXT.match(t)), "")
        price, vb = parse_price(price_text) if price_text else (None, False)
        location = next((t for t in texts if LOCATION_TEXT.match(t)), "")
        shipping = True if any(re.search(r"versand möglich", t, re.I) for t in texts) else None

        image = ld.get("contentUrl") or ""
        if not image:
            img = art.find("img")
            if img:
                srcset = img.get("srcset") or ""
                image = (srcset.split(",")[-1].strip().split(" ")[0] if srcset else "") or img.get("src") or ""

        out[ad_id] = Listing(
            source="kleinanzeigen",
            ad_id=ad_id,
            title=title[:200],
            url=canonical_url(m.group(1), ad_id) if m else f"https://www.kleinanzeigen.de{href}",
            price=price,
            negotiable=vb,
            description=strip_pii(description)[:2000],
            location=postcode_only(location)[:80],
            shipping=shipping,
            image_urls=[image] if image.startswith("https://") else [],
            raw_source="live",
        )
    return list(out.values())


class LiveSearch:
    def __init__(self, db: DB, http: httpx.Client | None = None, min_interval: int = MIN_INTERVAL_S,
                 daily_cap: int = DAILY_CAP, sleep=time.sleep):
        self.db = db
        self.http = http or httpx.Client(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
        self.min_interval = min_interval
        self.daily_cap = daily_cap
        self.sleep = sleep
        db.conn.execute("CREATE TABLE IF NOT EXISTS live_fetches (at REAL NOT NULL, url TEXT, status INTEGER)")
        db.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        db.conn.commit()

    # -- limits -------------------------------------------------------------
    def blocked_until(self) -> float:
        row = self.db.conn.execute("SELECT v FROM kv WHERE k='live_blocked_until'").fetchone()
        return float(row["v"]) if row else 0.0

    def _block(self, reason: str) -> None:
        until = time.time() + BACKOFF_S
        self.db.conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('live_blocked_until', ?)", (str(until),))
        self.db.conn.commit()
        raise LiveSearchBlocked(f"Kleinanzeigen refused the request ({reason}). Live search paused for 24 h; "
                                "use alert emails and the userscript meanwhile.")

    def used_today(self) -> int:
        row = self.db.conn.execute("SELECT COUNT(*) AS n FROM live_fetches WHERE at > ?", (time.time() - 86400,)).fetchone()
        return int(row["n"])

    def _wait_turn(self) -> None:
        if time.time() < self.blocked_until():
            left = (self.blocked_until() - time.time()) / 3600
            raise LiveSearchBlocked(f"live search paused after a block, {left:.1f} h left")
        if self.used_today() >= self.daily_cap:
            raise LiveSearchBlocked(f"daily cap of {self.daily_cap} live requests reached")
        row = self.db.conn.execute("SELECT MAX(at) AS t FROM live_fetches").fetchone()
        if row["t"]:
            wait = self.min_interval - (time.time() - row["t"])
            if wait > 0:
                log.info("rate limit: waiting %.0f s before next Kleinanzeigen request", wait)
                self.sleep(wait)

    # -- fetch --------------------------------------------------------------
    def search(self, url: str) -> FetchResult:
        if not url.startswith("https://www.kleinanzeigen.de/s-") or url.startswith("https://www.kleinanzeigen.de/s-anzeige/"):
            raise ValueError("only Kleinanzeigen search URLs are fetched")
        self._wait_turn()
        try:
            resp = self.http.get(url)
        except httpx.HTTPError as exc:
            self._log(url, 0)
            raise LiveSearchBlocked(f"network error: {exc}") from exc
        self._log(url, resp.status_code)
        final = str(resp.url)
        if resp.status_code in (403, 429) or "ip-eingeschraenkt" in final:
            self._block(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise LiveSearchBlocked(f"HTTP {resp.status_code} from Kleinanzeigen")
        listings = parse_search_html(resp.text)
        if not listings and BLOCK_HINTS.search(resp.text[:20000]):
            self._block("CAPTCHA / block page")
        return FetchResult(url=final, status=resp.status_code, listings=listings, html=resp.text)

    def _log(self, url: str, status: int) -> None:
        self.db.conn.execute("INSERT INTO live_fetches (at, url, status) VALUES (?,?,?)", (time.time(), url, status))
        self.db.conn.commit()
