"""Rule-based screening (German listing text). Used on its own when no LLM is
configured, and always applied on top of the LLM result as a safety net."""

from __future__ import annotations

import re

from . import catalog
from .models import Listing, Screen

# (regex, flag) — matched against lower-cased title + description.
RED_FLAG_PATTERNS: list[tuple[str, str]] = [
    (r"icloud|aktivierungssperre|activation\s?lock|apple[- ]?id\s+(gesperrt|vergessen)|google[- ]?(konto|sperre)|frp", "activation_lock"),
    (r"\bmdm\b|autopilot|von\s+(ihrer|deiner|einer)\s+organisation\s+verwaltet|managed by|intune|firmenverwaltung", "mdm_or_autopilot"),
    (r"bios[- ]?(passwort|password|pw|kennwort|gesperrt)|supervisor[- ]?pass|(passwort|kennwort)\s+(im|für das)\s+bios", "bios_password"),
    (r"inventar(nummer|aufkleber)|asset[- ]?tag|firmen(laptop|notebook|eigentum)|leasing(rückläufer|gerät)", "company_asset_tag"),
    (r"ohne\s+(netzteil|ladegerät|ladekabel|akku)|netzteil\s+fehlt|kein\s+netzteil", "no_charger"),
    (r"wasserschaden|flüssigkeit|kaffee\s+(drüber|darüber|ausgekippt)|liquid", "liquid_damage"),
    (r"muss\s+heute\s+weg|nur\s+heute|dringend.*(weg|verkauf)|schnell\s+weg", "suspicious_urgency"),
    (r"ohne\s+(ssd|festplatte|ram|arbeitsspeicher|display)|(ssd|festplatte|ram)\s+(fehlt|ausgebaut|entfernt)", "parts_missing"),
    (r"\b(nur\s+)?(karton|ovp|verpackung|hülle|tasche|netzteil)\s+(zu\s+verkaufen|ohne\s+gerät)|nur\s+(das\s+)?(gehäuse|mainboard|display)\b", "not_the_device"),
]

# (regex, category, p_ok, parts_eur, hours). First match wins, so order = severity.
FAULT_PATTERNS: list[tuple[str, str, float, float, float]] = [
    (r"wasserschaden|flüssigkeit|kaffee", "liquid", 0.25, 60, 2.5),
    (r"mainboard\s*(defekt|kaputt)|board\s*defekt|kein\s+bild.*(lüfter|led)|geht\s+nicht\s+(mehr\s+)?an|(?<!windows )startet\s+nicht|keine\s+reaktion|tot\b", "mainboard", 0.35, 40, 2.0),
    (r"display\s*(defekt|kaputt|gebrochen|gesprungen)|bildschirm\s*(defekt|kaputt|gebrochen)|streifen|displaybruch", "screen", 0.8, 60, 1.0),
    (r"scharnier|gehäuse\s*(gebrochen|defekt)|hinge", "hinge_case", 0.75, 30, 1.5),
    (r"lädt\s+nicht|ladebuchse|usb[- ]?c\s*(defekt|kaputt)|netzteilbuchse", "port_charging", 0.55, 25, 1.5),
    (r"tastatur|keyboard|tasten\s+(defekt|gehen\s+nicht)", "keyboard", 0.85, 35, 1.0),
    (r"akku\s*(defekt|kaputt|schwach|tot|hält\s+nicht)|battery", "battery", 0.9, 45, 0.5),
    (r"ssd|festplatte|hdd|bootet\s+nicht|no\s+boot(able)?\s+device|kein\s+betriebssystem", "storage", 0.8, 45, 1.0),
    (r"passwort\s+vergessen|kennwort\s+vergessen|windows\s*(fehler|startet\s+nicht|defekt|kaputt)|bluescreen|neu\s+aufsetzen|software", "software", 0.85, 0, 1.0),
    (r"defekt|bastler|kaputt|ersatzteil|for\s+parts|not\s+working", "unknown", 0.5, 40, 1.5),
]

STOCK_PHOTO_HINTS = r"symbolbild|beispielbild|herstellerbild|foto\s+aus\s+dem\s+internet"


def _text(listing: Listing) -> str:
    return f"{listing.title}\n{listing.description}\n{listing.condition}".lower()


# Title is about an accessory, not a device (docks, bags, chargers …).
_ACC = (r"docking\s?station|dockingstation|docstation|dock|port\s?replicator|\w*tasche|\w*rucksack|sleeve|\w*hülle|netzteil|"
        r"ladegerät|ladekabel|tastatur|maus|ersatzakku|displayschutz|halterung|ständer")
ACCESSORY_TITLE = re.compile(rf"\b({_ACC})\b", re.I)
ADDON_PHRASE = re.compile(rf"\b(mit|ohne|inkl\.?|inklusive|samt|plus|\+|und)\s+(\S+\s+){{0,3}}?({_ACC})\b", re.I)
MODEL_TITLE = re.compile(r"\b(thinkpad\s+[a-z]{1,2}\s?\d{2,4}s?|[txelp]\d{2,3}s?\b|latitude\s+\d{4}|elitebook\s+\d{3,4}|"
                         r"probook\s+\d{3}|v\s?\d{1,2}\b|macbook)", re.I)


def is_accessory(title: str) -> bool:
    core = ADDON_PHRASE.sub(" ", title)          # "T14 mit Netzteil" -> about the T14
    if not ACCESSORY_TITLE.search(core):
        return False
    subject = re.split(r"\b(?:für|passend|kompatibel|fits|for)\b", core, maxsplit=1, flags=re.I)[0]
    return not MODEL_TITLE.search(ACCESSORY_TITLE.sub(" ", subject))


def red_flags(listing: Listing) -> list[str]:
    text = _text(listing)
    flags = [flag for pattern, flag in RED_FLAG_PATTERNS if re.search(pattern, text)]
    if is_accessory(listing.title):
        flags.append("not_the_device")
    if re.search(STOCK_PHOTO_HINTS, text):
        flags.append("stock_photos")
    # BIOS password mentioned as *not* set ("kein BIOS-Passwort") is a good sign, not a flag.
    if "bios_password" in flags and re.search(r"(kein|ohne)\s+bios[- ]?(passwort|password|pw|kennwort)", text):
        flags.remove("bios_password")
    return sorted(set(flags))


def screen(listing: Listing) -> Screen:
    """Cheap deterministic screen; the LLM refines this when enabled."""
    text = _text(listing)
    entry = catalog.match(f"{listing.title} {listing.description[:300]}")
    category, p_ok, parts, hours = "none", 0.9, 0.0, 0.75
    for pattern, cat, p, cost, h in FAULT_PATTERNS:
        if re.search(pattern, text):
            category, p_ok, parts, hours = cat, p, cost, h
            break
    if entry:
        hours = max(hours, entry.repair_hours if category != "none" else 0.75)
    return Screen(
        device_model=entry.name if entry else "unknown",
        catalog_key=entry.key if entry else "none",
        fault_summary=f"rule match: {category}",
        fault_category=category,  # type: ignore[arg-type]
        p_ok=p_ok,
        parts_cost_eur=parts,
        repair_hours=hours,
        red_flags=red_flags(listing),  # type: ignore[arg-type]
        confidence="low",
        questions_for_seller=[],
    )


def merge(rule: Screen, llm: Screen) -> Screen:
    """LLM result wins, but rule-detected red flags are never dropped."""
    flags = sorted(set(llm.red_flags) | set(rule.red_flags))
    key = llm.catalog_key if catalog.by_key(llm.catalog_key) else rule.catalog_key
    return llm.model_copy(update={"red_flags": flags, "catalog_key": key})
