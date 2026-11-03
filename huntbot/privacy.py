"""GDPR helpers (plan section 4.4): keep seller data minimal before storing or sending to an LLM."""

from __future__ import annotations

import hashlib
import re

PHONE = re.compile(r"(?:\+49|0049|\b0)[\s/\-()]*\d{2,5}(?:[\s/\-()]*\d{2,})+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
STREET = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.|weg|platz|allee|gasse)\s+\d+[a-z]?\b")


def strip_pii(text: str) -> str:
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub("[telefon]", text)
    return STREET.sub("[adresse]", text)


def seller_hash(name_or_id: str, salt: str = "huntbot") -> str:
    if not name_or_id:
        return ""
    return hashlib.sha256(f"{salt}:{name_or_id}".encode()).hexdigest()[:16]


def postcode_only(location: str) -> str:
    """'80331 Altstadt-Lehel' -> '80331 Altstadt-Lehel'; drops anything that looks like a street."""
    return STREET.sub("", location).strip(" ,")


def text_fingerprint(title: str, description: str) -> str:
    norm = re.sub(r"\W+", " ", f"{title} {description[:400]}".lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()
