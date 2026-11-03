"""Turn a free-text hunt query into search terms for each source.

    "thinkpad t14 defekt bis 150€ versand"  ->  keywords, max price, shipping-only
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

# Typical German phrasings of broken / repairable listings, used to widen searches.
DEFECT_TERMS = ["defekt", "bastler", "kaputt", "ersatzteile", "passwort vergessen", "startet nicht"]

SYNONYMS = {
    "laptop": ["laptop", "notebook"],
    "notebook": ["notebook", "laptop"],
    "staubsauger": ["staubsauger", "akkusauger"],
}


@dataclass
class HuntQuery:
    raw: str
    keywords: list[str]
    max_price: float | None = None
    min_price: float | None = None
    shipping_only: bool = False
    broken_only: bool = False
    variants: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.keywords)


PRICE_MAX = re.compile(r"(?:bis|max\.?|unter|<|≤|<=)\s*(\d+(?:[.,]\d+)?)\s*(?:€|eur|euro)?", re.I)
PRICE_MIN = re.compile(r"(?:ab|min\.?|über|>|>=|≥)\s*(\d+(?:[.,]\d+)?)\s*(?:€|eur|euro)?", re.I)
PRICE_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*(?:€|eur|euro)", re.I)
BROKEN = re.compile(r"\b(defekt|bastler|kaputt|broken|for parts)\b", re.I)


def parse(raw: str) -> HuntQuery:
    text = raw.strip()
    q = HuntQuery(raw=raw, keywords=[])
    if m := PRICE_RANGE.search(text):
        q.min_price, q.max_price = float(m.group(1)), float(m.group(2))
        text = text.replace(m.group(0), " ")
    if m := PRICE_MAX.search(text):
        q.max_price = float(m.group(1).replace(",", "."))
        text = text.replace(m.group(0), " ")
    if m := PRICE_MIN.search(text):
        q.min_price = float(m.group(1).replace(",", "."))
        text = text.replace(m.group(0), " ")
    if re.search(r"\b(nur\s+)?versand\b|\bshipping\b", text, re.I):
        q.shipping_only = True
        text = re.sub(r"\b(nur\s+)?versand\b|\bshipping\b", " ", text, flags=re.I)
    q.broken_only = bool(BROKEN.search(text))
    q.keywords = [w for w in re.split(r"\s+", text.replace("€", " ")) if w]
    q.variants = expand(q)
    return q


def expand(q: HuntQuery, limit: int = 6) -> list[str]:
    """Search-string variants: the query itself, plus defect phrasings if not already broken-only."""
    base = q.text
    variants = [base]
    for word in q.keywords:
        for syn in SYNONYMS.get(word.lower(), []):
            alt = base.replace(word, syn)
            if alt not in variants:
                variants.append(alt)
    if not q.broken_only:
        core = base
        variants += [f"{core} {term}" for term in DEFECT_TERMS[:3]]
    return variants[:limit]


def _slug(text: str) -> str:
    text = text.lower().strip()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def kleinanzeigen_url(phrase: str, q: HuntQuery, location_slug: str = "", location_id: str = "", radius_km: int = 0) -> str:
    """Search URL for the user's own browser. Path format (unverified, check once):
    /s-<ort>/preis:<min>:<max>/<suchbegriff>/k0l<ortId>r<radius>
    """
    parts = ["https://www.kleinanzeigen.de"]
    if location_slug and location_id:
        parts.append(f"s-{location_slug}")
        if q.min_price or q.max_price:
            parts.append(f"preis:{int(q.min_price or 0) or ''}:{int(q.max_price) if q.max_price else ''}")
        parts.append(_slug(phrase))
        parts.append(f"k0l{location_id}" + (f"r{radius_km}" if radius_km else ""))
    else:
        if q.min_price or q.max_price:
            parts.append(f"s-preis:{int(q.min_price or 0) or ''}:{int(q.max_price) if q.max_price else ''}")
            parts.append(_slug(phrase))
        else:
            parts.append(f"s-{_slug(phrase)}")
        parts.append("k0")
    return "/".join(parts)


def ebay_web_sold_url(phrase: str) -> str:
    """eBay.de 'sold items' search for manual price checks (feeds data/catalog.csv)."""
    return "https://www.ebay.de/sch/i.html?" + urlencode({"_nkw": phrase, "LH_Sold": 1, "LH_Complete": 1})


def ebay_filter(q: HuntQuery, parts_only: bool = True) -> str:
    filters = ["itemLocationCountry:DE", "priceCurrency:EUR"]
    if parts_only:
        filters.append("conditionIds:{7000}")  # For parts or not working
    if q.min_price or q.max_price:
        filters.append(f"price:[{int(q.min_price or 0)}..{int(q.max_price) if q.max_price else ''}]")
    return ",".join(filters)


def fts_query(q: HuntQuery) -> str:
    """SQLite FTS5 MATCH expression: every keyword must appear (prefix match)."""
    terms = [re.sub(r"[^\w]", "", w) for w in q.keywords]
    terms = [t for t in terms if t and t.lower() not in {"defekt", "bastler", "kaputt"}]
    return " AND ".join(f'"{t}"*' for t in terms) if terms else ""


__all__ = ["HuntQuery", "parse", "kleinanzeigen_url", "ebay_filter", "ebay_web_sold_url", "fts_query", "quote"]
