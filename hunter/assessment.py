"""What the screener (Claude or keyword rules) says about one listing.

These Pydantic models double as Claude structured-output schemas, so they avoid
field constraints; values are clamped in `clamp()` instead.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from hunter.models import Listing
from hunter.prices import PriceTable

RedFlag = Literal[
    "activation_lock",  # iCloud / Find My / Microsoft account lock
    "mdm_or_autopilot",  # "managed by your organisation", Autopilot, Intune
    "bios_password",  # supervisor / BIOS password, Computrace activated
    "stock_photos",  # no real photos of the actual unit
    "company_asset_tag",  # inventory sticker, leasing label, no proof of purchase
    "no_charger",
    "possibly_stolen",  # urgent + cheap + no history, ground-down serial, etc.
    "mainboard_damage",  # no sign of life, board fault, short
    "liquid_damage",
    "commercial_seller",
    "not_a_device",  # accessory, spare part, box only, wanted ad
]

REJECT_FLAGS = {"activation_lock", "mdm_or_autopilot", "possibly_stolen", "not_a_device"}
EV_MULTIPLIERS = {"bios_password": 0.5, "company_asset_tag": 0.5, "stock_photos": 0.7}
CHARGER_COST_EUR = 15.0


class Assessment(BaseModel):
    relevant: bool
    model_key: str  # a key from prices.yaml, or "unknown"
    model_guess: str  # free text, e.g. "ThinkPad T14 Gen 2, i5-1135G7, 16 GB"
    fault_summary: str
    likely_fix: str
    parts_cost_eur: float
    repair_hours: float
    p_ok: float  # probability the likely fix makes the unit sellable
    red_flags: list[RedFlag]
    confidence: float  # 0..1, how sure the screener is about model + fault
    notes: str

    def clamp(self, p_ok_cap: float) -> "Assessment":
        self.p_ok = max(0.0, min(self.p_ok, p_ok_cap))
        self.confidence = max(0.0, min(self.confidence, 1.0))
        self.parts_cost_eur = max(0.0, self.parts_cost_eur)
        self.repair_hours = max(0.1, self.repair_hours)
        self.red_flags = list(dict.fromkeys(self.red_flags))
        return self


class Review(Assessment):
    """Deep review: everything in Assessment plus a negotiation draft."""

    offer_eur: float
    offer_message_de: str


# ---------------------------------------------------------------------------
# Keyword rules: used when Claude is off, and as a cheap pre-filter.
# ---------------------------------------------------------------------------

_FLAG_RULES: list[tuple[str, str]] = [
    (r"icloud|aktivierungssperre|activation\s*lock|microsoft[- ]konto gesperrt", "activation_lock"),
    (r"autopilot|\bmdm\b|intune|verwaltet von|managed by|ihre organisation", "mdm_or_autopilot"),
    (r"bios[- ]?(passwort|kennwort|gesperrt|pw)|supervisor|computrace", "bios_password"),
    (r"inventar|asset[- ]?tag|leasing|firmenger(ä|ae)t|firmeneigentum", "company_asset_tag"),
    (r"ohne\s+(netzteil|ladeger(ä|ae)t|ladekabel)|kein(en)?\s+(netzteil|ladeger(ä|ae)t|ladekabel)", "no_charger"),
    (r"kein(e)?\s+lebenszeichen|keine\s+reaktion|mainboard|platine|kurzschluss|geht\s+nicht\s+an|l(ä|ae)sst sich nicht einschalten", "mainboard_damage"),
    (r"wasserschaden|fl(ü|ue)ssigkeit|kaffee\s*(drüber|darüber|verschüttet)|getr(ä|ae)nk", "liquid_damage"),
    (r"\bsuche\b|\bgesucht\b|nur\s+(die\s+)?(tastatur|akku|netzteil|h(ü|ue)lle|tasche)\b|\bleergeh(ä|ae)use\b|\bovp\s+only\b", "not_a_device"),
]

# (pattern, p_ok, parts €) - first match wins, most fixable faults first
_FAULT_RULES: list[tuple[str, float, float, str]] = [
    (r"passwort|kennwort|pin vergessen|windows[- ](fehler|startet nicht|bootet nicht)|bluescreen|neu (aufsetzen|installieren)", 0.85, 0.0, "reinstall Windows / reset password"),
    (r"ohne\s+(ssd|festplatte|hdd)|ssd\s+defekt|festplatte\s+defekt|m\.?2\b", 0.8, 45.0, "fit a new NVMe SSD"),
    (r"akku", 0.85, 45.0, "replace battery"),
    (r"tastatur", 0.85, 35.0, "replace keyboard"),
    (r"display|bildschirm|lcd|panel", 0.75, 60.0, "replace screen"),
    (r"scharnier", 0.7, 25.0, "replace hinge"),
    (r"usb[- ]?c|ladebuchse|l(ä|ae)dt nicht", 0.6, 25.0, "replace USB-C / charging board"),
    (r"filter|zyklon|b(ü|ue)rste|saugt nicht", 0.8, 20.0, "clean / replace filter and brush"),
]


_PART_WORDS = (r"tastatur|keyboard|akku|batterie|netzteil|ladeger(ä|ae)t|ladekabel|l(ü|ue)fter|k(ü|ue)hler|"
               r"mainboard|motherboard|geh(ä|ae)use|palmrest|bodenplatte|displaydeckel|scharnier|"
               r"dockingstation|docking|tasche|h(ü|ue)lle|ram|arbeitsspeicher|ssd|festplatte")
_DEVICE_WORDS = r"laptop|notebook|ultrabook|convertible|staubsauger|sauger"


def looks_like_part_only(listing: Listing) -> bool:
    """'Original Lenovo Thinkpad Tastatur T14 defekt' -> True; 'ThinkPad T480 Akku defekt' -> False."""
    typ = (listing.attributes.get("Typ") or listing.attributes.get("Art") or "").lower()
    if typ and not re.search(r"laptop|notebook|staubsauger|sauger", typ):
        return True  # e.g. "Zubehör"; "Laptop" is not trusted - sellers pick it for parts too
    title = listing.title.lower()
    part = re.search(rf"\b({_PART_WORDS})\b", title)
    if not part or re.search(rf"\b({_DEVICE_WORDS})\b", title):
        return False
    if re.search(r"\bf(ü|ue)r\b", title):  # "Akku für ThinkPad T480"
        return True
    model_tok = re.search(r"\b[a-z]{0,3}\d{2,4}[a-z]?\b", title)  # T14, X1, E480, V11 ...
    return model_tok is not None and part.start() < model_tok.start()


def rules_assessment(listing: Listing, prices: PriceTable, p_ok_cap: float = 0.9) -> Assessment:
    text = f"{listing.title}\n{listing.description}"
    low = text.lower()
    model = prices.match(text)
    flags = [flag for pattern, flag in _FLAG_RULES if re.search(pattern, low)]
    if looks_like_part_only(listing) and "not_a_device" not in flags:
        flags.append("not_a_device")
    if listing.seller_type == "commercial":
        flags.append("commercial_seller")

    p_ok, parts, fix = 0.55, (model.default_parts if model else 40.0), "unclear fault - ask the seller"
    for pattern, rule_p, rule_parts, rule_fix in _FAULT_RULES:
        if re.search(pattern, low):
            p_ok, parts, fix = rule_p, rule_parts, rule_fix
            break
    if "mainboard_damage" in flags:
        p_ok, fix = min(p_ok, 0.3), "board-level fault - likely parts unit"
    if "liquid_damage" in flags:
        p_ok = min(p_ok, 0.35)

    return Assessment(
        relevant="not_a_device" not in flags,
        model_key=model.key if model else "unknown",
        model_guess=model.label if model else listing.title[:80],
        fault_summary=fix if fix else "",
        likely_fix=fix,
        parts_cost_eur=parts,
        repair_hours=model.repair_hours if model else 1.0,
        p_ok=p_ok,
        red_flags=flags,
        confidence=0.4 if model else 0.2,
        notes="keyword rules (Claude off)",
    ).clamp(p_ok_cap)
