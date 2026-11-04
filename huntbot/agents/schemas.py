"""Pydantic schemas for the three LLM agents and the code-made Verdict, plus strict-JSON-schema helpers.

Every field is required (strict structured outputs need that); `DEFAULTS` supplies lenient fill values
for the JSON repair step. Agents never output money used by the code, except `stated_purchase_price_eur`
(accepted only with a verified quote containing the digits).
"""

from __future__ import annotations

import copy
import typing
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from ..models import FaultCategory

Conf = Literal["low", "medium", "high"]


class ReaderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_type: Literal["device", "device_bundle", "part", "accessory", "box_or_docs",
                       "wanted_ad", "multiple_devices", "unclear"]
    item_type_evidence: list[str]
    device_model: str
    cpu: str
    generation: str
    ram_gb: int
    ssd_gb: int
    working_status: Literal["working", "working_minor_issues", "broken", "untested", "unknown"]
    working_evidence: list[str]
    fault_category: FaultCategory
    fault_summary: str
    condition: Literal["new", "like_new", "good", "worn", "defect", "unknown"]
    charger_included: Literal["yes", "no", "unknown"]
    stated_purchase_price_eur: float
    stated_purchase_date: str
    purchase_evidence: list[str]
    seller_type: Literal["private", "dealer", "unknown"]
    seller_evidence: list[str]
    price_is_for_this_item: bool
    price_evidence: list[str]
    lock_signals: list[Literal["activation_lock", "mdm_or_autopilot", "bios_password",
                               "windows_password", "company_asset", "computrace"]]
    lock_evidence: list[str]
    scam_signals: list[Literal["paypal_friends", "prepayment_only", "abroad_shipping_only",
                               "off_platform_contact", "stock_photos_admitted", "paypal_only"]]
    scam_evidence: list[str]
    missing_parts: list[Literal["ssd", "ram", "battery", "charger", "display", "keyboard", "other"]]
    questions_for_seller: list[str]
    confidence: Conf


class InspectorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    images_reviewed: int
    photo_kind: Literal["real_device", "stock_or_render", "part_only", "accessory_only",
                        "box_or_docs", "screenshot_or_text", "unclear"]
    matches_title: Literal["yes", "no", "unclear"]
    screen_state: Literal["off_or_not_visible", "desktop_or_bios_setup", "windows_login",
                          "bios_password_prompt", "organisation_or_activation_lock",
                          "error_or_no_boot", "damaged_display", "unclear"]
    visible_damage: list[Literal["cracked_screen", "display_lines_or_spots", "broken_hinge",
                                 "case_cracks", "missing_keys", "liquid_marks", "heavy_wear"]]
    asset_tag_or_inventory_sticker: bool
    multiple_units_visible: bool
    notes: str
    confidence: Conf


class Objection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["not_the_device", "broken", "scam", "stolen_or_locked", "wrong_model",
                  "dealer", "thin_info", "overpriced", "other"]
    claim: str
    evidence: str
    severity: Literal["low", "medium", "high"]


class SkepticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objections: list[Objection]
    verdict: Literal["no_blocker", "downgrade_to_watch", "veto"]
    questions_for_seller: list[str]


Decision = Literal["BUY", "WATCH", "PASS", "PENDING", "UNSCREENED"]


class Verdict(BaseModel):
    """Produced by code (judge.py), stored as JSON. `detail` carries what the card/web show."""
    decision: Decision
    confidence: Conf = "low"
    reasons: list[str] = []
    vetoes: list[str] = []
    agents_run: list[str] = []
    models_used: dict[str, str] = {}
    calls_used: int = 0
    degraded: bool = False
    cost_usd: float = 0.0
    photo: str = ""                  # "real_device, windows_login, damage: ..."
    objection: str = ""              # skeptic's top claim
    questions: list[str] = []


# -- JSON schema (strict-friendly) -------------------------------------------------------------

def json_schema(model_cls: type[BaseModel]) -> dict:
    schema = copy.deepcopy(model_cls.model_json_schema())
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            node.pop("title", None)
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for k, v in list(node.items()):
                node[k] = walk(v)
        elif isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _unknown_member(tp: Any) -> Any:
    options = list(get_args(tp))
    for cand in ("unknown", "unclear", "none", "other", "low"):
        if cand in options:
            return cand
    return options[0]


def _default_for(name: str, annotation: Any) -> Any:
    if name == "price_is_for_this_item":
        return True
    if name == "confidence":
        return "low"
    if name == "verdict":
        return "downgrade_to_watch"
    origin = get_origin(annotation)
    if origin is typing.Literal:
        return _unknown_member(annotation)
    if origin is list:
        return []
    if annotation is bool:
        return False
    if annotation in (int, float):
        return 0
    if annotation is str:
        return ""
    return None


DEFAULTS: dict[type[BaseModel], dict[str, Any]] = {
    cls: {n: _default_for(n, f.annotation) for n, f in cls.model_fields.items()}
    for cls in (ReaderOutput, InspectorOutput, SkepticOutput, Objection)
}
