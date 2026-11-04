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
    # Rules v2: deterministic catches for scams and dealers (0 LLM calls).
    (r"paypal\s*(an\s+)?(freunde|familie|friends|f\s?&\s?f)|nur\s+(per\s+)?(vorkasse|überweisung)|western\s+union|"
     r"(bin|wohne)\s+(\w+\s+){0,2}(im|in\s+der)\s+ausland|nur\s+versand[^.]{0,40}kein\w*\s+abholung", "scam_pattern"),
    (r"\bab\s+\d{2,4}\s?(€|eur)|verschiedene[n]?\s+konfiguration|gewerbliche[rn]?\s+(händler|verkäufer|anbieter)|unsere\s+vorteile|"
     r"mwst\.?\s+ausweisbar|rechnung\s+mit\s+(ausgewiesener\s+)?mwst", "dealer_listing"),
]

# (regex, category, p_ok, parts_eur, hours). First match wins, so order = severity.
_BAD = r"(defekt|kaputt|tot|fehlt|fehlen|gebrochen|beschädigt|geht\s+nicht|gehen\s+nicht|funktioniert\s+nicht|ausgebaut)"
FAULT_PATTERNS: list[tuple[str, str, float, float, float]] = [
    (r"wasserschaden|flüssigkeit\w*\s+(drüber|darüber|eingedrungen)|kaffee\s+(drüber|darüber|ausgekippt)", "liquid", 0.25, 60, 2.5),
    (r"mainboard\s*(defekt|kaputt)|board\s*defekt|kein\s+bild.*(lüfter|led)|geht\s+nicht\s+(mehr\s+)?an|(?<!windows )startet\s+nicht|"
     r"keine\s+reaktion|\btot\b|springt\s+nicht|springt\s+(nur\s+)?(\w+\s+)?sporadisch|nur\s+sporadisch|"
     r"geht\s+(immer\s+)?(wieder\s+)?aus", "mainboard", 0.35, 40, 2.0),
    (rf"(display|bildschirm|lcd)\s*{_BAD}|streifen\s+im\s+(bild|display)|displaybruch|pixelfehler", "screen", 0.8, 60, 1.0),
    (rf"(scharnier|gehäuse|hinge)\w*\s*{_BAD}|scharnier\s+locker", "hinge_case", 0.75, 30, 1.5),
    (rf"lädt\s+nicht|(ladebuchse|usb[- ]?c|netzteilbuchse)\s*{_BAD}", "port_charging", 0.55, 25, 1.5),
    (rf"(tastatur|keyboard|tasten|trackpad|touchpad)\s*{_BAD}", "keyboard", 0.85, 35, 1.0),
    (rf"(akku|battery|batterie)\s*{_BAD}|akku\s+(schwach|hält\s+nicht|hält\s+nur)|akku\s+aufgebläht", "battery", 0.9, 45, 0.5),
    (rf"(ssd|festplatte|hdd|m\.?2)\s*{_BAD}|ohne\s+(ssd|festplatte|hdd)|bootet\s+nicht|no\s+boot(able)?\s+device|kein\s+betriebssystem", "storage", 0.8, 45, 1.0),
    (r"passwort\s+vergessen|kennwort\s+vergessen|windows\s*(fehler|startet\s+nicht|defekt|kaputt)|bluescreen|neu\s+aufsetzen", "software", 0.85, 0, 1.0),
    (r"defekt|bastler|kaputt|ersatzteil|for\s+parts|not\s+working|zum\s+ausschlachten|reparieren|reparatur\w*\s+nötig|"
     r"muss\s+repariert|funktioniert\s+(leider\s+)?nicht|nicht\s+(mehr\s+)?funktionsfähig|keine\s+funktion|"
     r"ungetestet|nicht\s+getestet", "unknown", 0.5, 40, 1.5),
]

STOCK_PHOTO_HINTS = r"symbolbild|beispielbild|herstellerbild|foto\s+aus\s+dem\s+internet"


def _text(listing: Listing) -> str:
    return f"{listing.title}\n{listing.description}\n{listing.condition}".lower()


# Title is about an accessory or a spare part, not a device.
_ACC = (r"docking\s?station|dockingstation|docstation|dock|port\s?replicator|\w*tasche|\w*rucksack|sleeve|\w*hülle|"
        r"netzteil|ladegerät|ladekabel|maus|displayschutz\w*|blickschutz\w*|\w*filter|halterung|ständer|stylus|"
        r"\w*folie|skin|aufkleber")
_PART = (r"tastatur|keyboard|display(?:einheit|panel)?|panel|lcd|bildschirm|akku|batterie|mainboard|motherboard|"
         r"lüfter|kühler|scharnier\w*|gehäuse\w*|bodenplatte|palmrest|handauflage|touchpad|ram[- ]?riegel|"
         r"arbeitsspeicher|ersatzteil\w*|ersatzakku")
ACCESSORY_TITLE = re.compile(rf"\b({_ACC})\b", re.I)
PART_TITLE = re.compile(rf"\b({_PART})\b", re.I)
ADDON_PHRASE = re.compile(rf"\b(mit|ohne|inkl\.?|inklusive|samt|plus|neue[mnrs]?|\+|und)\s+(\S+\s+){{0,3}}?({_ACC}|{_PART})\b", re.I)
FAULT_AFTER = re.compile(rf"\b({_PART})\w*\s*(defekt|kaputt|gebrochen|beschädigt|fehlt|geht|hält|schwach|tot|locker|streifen)", re.I)
PART_SPEC_AFTER = re.compile(rf"\b({_PART})\w*\s*(neu|neuwertig|getauscht|erneuert|gewechselt|ok|gut|top|voll|\d{{1,3}}\s?%)", re.I)
DEVICE_WORD = re.compile(r"\b(laptop|notebook|ultrabook|convertible|2[- ]in[- ]1)\b", re.I)


def is_accessory(title: str) -> bool:
    """Accessory (dock, bag, charger, filter) or spare part (keyboard, panel, battery) sold on its own."""
    core = ADDON_PHRASE.sub(" ", title)          # "T14 mit Netzteil" -> about the T14
    if ACCESSORY_TITLE.search(core):
        return True
    if PART_TITLE.search(core):
        # "T480 Tastatur defekt" / "Notebook, Display gebrochen" = a device with a fault
        return not (FAULT_AFTER.search(core) or PART_SPEC_AFTER.search(core) or DEVICE_WORD.search(core))
    return False


def red_flags(listing: Listing) -> list[str]:
    text = _text(listing)
    flags = [flag for pattern, flag in RED_FLAG_PATTERNS if re.search(pattern, text)]
    if is_accessory(listing.title):
        flags.append("not_the_device")
    if re.match(r"\s*(suche|gesucht|tausche)\b", listing.title, re.I):
        flags.append("wanted_ad")
    if re.search(STOCK_PHOTO_HINTS, text):
        flags.append("stock_photos")
    # BIOS password mentioned as *not* set ("kein BIOS-Passwort") is a good sign, not a flag.
    if "bios_password" in flags and re.search(r"(kein|ohne)\s+bios[- ]?(passwort|password|pw|kennwort)", text):
        flags.remove("bios_password")
    return sorted(set(flags))


# category -> (p_ok, parts_eur, hours): the first pattern per category in FAULT_PATTERNS, plus "none".
FAULT_DEFAULTS: dict[str, tuple[float, float, float]] = {"none": (0.9, 0.0, 0.75)}
for _pat, _cat, _p, _cost, _h in FAULT_PATTERNS:
    FAULT_DEFAULTS.setdefault(_cat, (_p, _cost, _h))


def defaults_for(category: str, entry: "catalog.CatalogEntry | None" = None) -> tuple[float, float, float]:
    """(p_ok, parts_eur, hours) for a fault category: code-owned, never taken from an LLM."""
    p_ok, parts, hours = FAULT_DEFAULTS.get(category, FAULT_DEFAULTS["unknown"])
    if entry:
        hours = max(hours, entry.repair_hours if category != "none" else 0.75)
    return p_ok, parts, hours


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
