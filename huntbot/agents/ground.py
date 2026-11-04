"""Evidence grounding: an LLM fact only counts if its verbatim quote appears in the listing text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..models import Listing
from .schemas import ReaderOutput


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub("[„“”\"'’`´]", '"', s)
    s = re.sub("[–—]", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("…").removeprefix("...").removesuffix("...").strip()


def listing_text(l: Listing) -> str:
    return f"{l.title}\n{l.description}\n{l.condition}"


MAX_ELLIPSIS_GAP = 80     # "a … b": b must start within 80 chars after a
MIN_ELLIPSIS_PART = 6     # each piece must be a real phrase, not "nicht"
NEGATION_AFTER = re.compile(r"^\W{0,3}(nicht|nie|kaum|nur\s+noch|leider\s+nicht|mehr\s+nicht|keine?)\b")


def verified(quote: str, text: str) -> bool:
    q = norm(quote)
    if len(q) < 4:
        return False
    t = norm(text)
    if "…" in q or "..." in q:
        parts = [p.strip() for p in re.split(r"…|\.\.\.", q) if p.strip()]
        if not parts or any(len(p) < MIN_ELLIPSIS_PART for p in parts):
            return False   # no stitching of short words ("nicht … defekt")
        pos, prev_end = 0, None
        for part in parts:
            i = t.find(part, pos)
            if i < 0 or (prev_end is not None and i - prev_end > MAX_ELLIPSIS_GAP):
                return False
            pos = prev_end = i + len(part)
        return True
    return q in t


def verified_affirmative(quote: str, text: str) -> bool:
    """For positive claims ("funktioniert einwandfrei"): verbatim AND not cut off before a negation
    ("Laptop funktioniert | nicht mehr"). Every occurrence must be un-negated."""
    if not verified(quote, text):
        return False
    q, t = norm(quote).split("…")[-1].split("...")[-1].strip(), norm(text)
    starts = [m.start() for m in re.finditer(re.escape(q), t)] or [t.find(q)]
    return all(not NEGATION_AFTER.search(t[s + len(q): s + len(q) + 25]) for s in starts if s >= 0)


def any_verified(quotes: list[str], text: str) -> bool:
    return any(verified(q, text) for q in quotes)


def _digits(s: str) -> set[float]:
    out = set()
    for m in re.finditer(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", s):
        raw = m.group(0)
        if re.search(r"[.,]\d{3}(?![\d])", raw) and not re.search(r"[.,]\d{1,2}$", raw):
            raw = re.sub(r"[.,](\d{3})", r"\1", raw)
        elif re.search(r"[.,]\d{3}[.,]", raw):
            raw = re.sub(r"[.,](\d{3})(?=[.,])", r"\1", raw)
        try:
            out.add(float(raw.replace(",", ".")))
        except ValueError:
            pass
    return out


def verify_quote_number(quotes: list[str], value: float, text: str, tol: float = 1.0) -> bool:
    """A verified quote must contain the number (±tol €)."""
    for q in quotes:
        if verified(q, text) and any(abs(v - value) <= tol for v in _digits(q)):
            return True
    return False


@dataclass
class Grounding:
    item_type: bool = False
    working: bool = False
    purchase: bool = False
    purchase_number: bool = False
    seller: bool = False
    price: bool = False
    lock: bool = False
    scam: bool = False
    total: int = 0
    ok: int = 0
    bad_quotes: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.ok / self.total if self.total else 1.0


def verify_reader(r: ReaderOutput, l: Listing) -> Grounding:
    text = listing_text(l)
    g = Grounding()
    groups = {"item_type": r.item_type_evidence, "working": r.working_evidence, "purchase": r.purchase_evidence,
              "seller": r.seller_evidence, "price": r.price_evidence, "lock": r.lock_evidence, "scam": r.scam_evidence}
    positive_working = r.working_status in ("working", "working_minor_issues")
    for name, quotes in groups.items():
        check = verified_affirmative if (name == "working" and positive_working) else verified
        hits = [check(q, text) for q in quotes]
        g.total += len(hits)
        g.ok += sum(hits)
        g.bad_quotes += [q for q, h in zip(quotes, hits) if not h]
        setattr(g, name, any(hits))
    if r.stated_purchase_price_eur > 0:
        g.purchase_number = verify_quote_number(r.purchase_evidence, r.stated_purchase_price_eur, text)
    return g
