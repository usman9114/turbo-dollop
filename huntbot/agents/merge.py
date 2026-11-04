"""Merger: turn verified agent facts into Screen / Features. Code owns p_ok, parts and hours (never an LLM)."""

from __future__ import annotations

from dataclasses import replace

from .. import catalog, features, rules
from ..models import Listing, Screen
from .ground import Grounding, listing_text, verify_quote_number
from .schemas import InspectorOutput, ReaderOutput

CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _with_category(screen: Screen, category: str) -> Screen:
    entry = catalog.by_key(screen.catalog_key)
    p_ok, parts, hours = rules.defaults_for(category, entry)
    return screen.model_copy(update={"fault_category": category, "p_ok": p_ok, "parts_cost_eur": parts,
                                     "repair_hours": hours})


def apply_reader(rule: Screen, feats: features.Features, r: ReaderOutput, g: Grounding, l: Listing,
                 ) -> tuple[Screen, features.Features, list[str]]:
    """Returns (screen, features, soft_signals). Unverified negatives become soft signals, never flags."""
    soft: list[str] = []
    screen = rule

    # -- working status / fault
    cat = rule.fault_category
    if r.working_status == "broken":
        if g.working:
            cat = r.fault_category if r.fault_category != "none" else "unknown"   # broken always wins
        else:
            soft.append("unverified:broken")
    elif r.working_status in ("working", "working_minor_issues"):
        if not g.working:
            soft.append("unverified:working")
        elif cat in ("none", "unknown") and CONF_RANK[r.confidence] >= 1:
            cat = "none"
        elif cat not in ("none", "unknown"):
            soft.append("conflict:working")
    elif r.working_status == "untested":
        soft.append("untested")
    if cat != rule.fault_category:
        screen = _with_category(screen, cat)
    if r.lock_signals and "windows_password" in r.lock_signals and screen.fault_category == "none" and r.working_status != "broken":
        screen = _with_category(screen, "software")

    # -- identity
    device = r.device_model if r.device_model.strip().lower() not in ("", "unknown") else screen.device_model
    entry = catalog.match(f"{r.device_model} {l.title}") or catalog.by_key(rule.catalog_key)
    screen = screen.model_copy(update={"device_model": device, "catalog_key": entry.key if entry else "none"})
    if entry and screen.fault_category != "none":      # repair hours depend on the catalog row
        screen = _with_category(screen, screen.fault_category)

    # -- red flags: rule flags + verified mapped Reader flags
    flags = set(screen.red_flags)

    def add(flag: str, ok: bool) -> None:
        if ok:
            flags.add(flag)
        else:
            soft.append(f"unverified:{flag}")

    if r.item_type in ("part", "accessory", "box_or_docs"):
        add("not_the_device", g.item_type)
    if r.item_type == "wanted_ad":
        add("wanted_ad", g.item_type)
    if r.seller_type == "dealer":
        add("dealer_listing", g.seller)
    if any(s in r.scam_signals for s in ("paypal_friends", "prepayment_only", "abroad_shipping_only", "off_platform_contact")):
        add("scam_pattern", g.scam)
    if "stock_photos_admitted" in r.scam_signals:
        add("stock_photos", g.scam)
    lock_map = {"activation_lock": "activation_lock", "mdm_or_autopilot": "mdm_or_autopilot",
                "computrace": "mdm_or_autopilot", "bios_password": "bios_password", "company_asset": "company_asset_tag"}
    for sig in r.lock_signals:
        if sig in lock_map:
            add(lock_map[sig], g.lock)
    if r.charger_included == "no":
        flags.add("no_charger")
    if any(p in r.missing_parts for p in ("ssd", "ram", "display", "keyboard")):
        flags.add("parts_missing")
    screen = screen.model_copy(update={"red_flags": sorted(flags)})

    # -- features: fill gaps only, regex output wins
    text = listing_text(l)
    f = feats
    if f.year is None:
        f = replace(f, year=features._first_year(f"{r.device_model} {r.cpu} {r.generation}".lower()))
    if f.ram_gb is None and 2 <= r.ram_gb <= 64:
        f = replace(f, ram_gb=float(r.ram_gb))
    if f.ssd_gb is None and 64 <= r.ssd_gb <= 8192:
        f = replace(f, ssd_gb=float(r.ssd_gb))
    if f.retail_price is None and r.stated_purchase_price_eur > 0 and 30 <= r.stated_purchase_price_eur <= 20000 \
            and verify_quote_number(r.purchase_evidence, r.stated_purchase_price_eur, text):
        f = replace(f, retail_price=float(r.stated_purchase_price_eur))
    if f.condition == "good" and r.condition in ("new", "like_new", "worn") and (g.working or g.purchase or g.item_type):
        f = replace(f, condition=r.condition)
    if screen.fault_category != "none":
        f = replace(f, condition="defect")
    if f.charger is None and r.charger_included in ("yes", "no"):
        f = replace(f, charger=(r.charger_included == "yes"))

    screen = screen.model_copy(update={
        "fault_summary": ("team: " + r.fault_summary) if r.fault_summary else screen.fault_summary,
        "confidence": r.confidence,
        "questions_for_seller": r.questions_for_seller[:3],
    })
    return screen, f, soft


def apply_inspector(screen: Screen, i: InspectorOutput, l: Listing) -> tuple[Screen, list[str]]:
    soft: list[str] = []
    if i.confidence == "low" or i.images_reviewed == 0:
        return screen, soft
    flags = set(screen.red_flags)
    if i.photo_kind == "stock_or_render":
        flags.add("stock_photos")
    if i.screen_state == "bios_password_prompt":
        flags.add("bios_password")
    if i.screen_state == "organisation_or_activation_lock":
        if i.confidence == "high":
            flags.add("mdm_or_autopilot")
        else:
            soft.append("vision:lock?")
    if i.asset_tag_or_inventory_sticker:
        flags.add("company_asset_tag")
    if i.photo_kind in ("part_only", "accessory_only", "box_or_docs"):
        soft.append("vision:not_device")      # a thumbnail can show only the charger: Reader must agree for PASS
    if i.matches_title == "no":
        soft.append("vision:mismatch")
    if i.multiple_units_visible:
        soft.append("vision:stock")
    damaged = i.screen_state == "damaged_display" or any(d in ("cracked_screen", "display_lines_or_spots") for d in i.visible_damage)
    if damaged and screen.fault_category == "none":
        entry = catalog.by_key(screen.catalog_key)
        p_ok, parts, hours = rules.defaults_for("screen", entry)
        screen = screen.model_copy(update={"fault_category": "screen", "p_ok": p_ok, "parts_cost_eur": parts, "repair_hours": hours})
        soft.append("conflict:vision_damage")
    return screen.model_copy(update={"red_flags": sorted(flags)}), soft
