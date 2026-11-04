import json

import pytest

from huntbot.agents.jsonparse import ParseError, parse
from huntbot.agents.schemas import DEFAULTS, InspectorOutput, ReaderOutput, SkepticOutput, json_schema

from .agents_fakes import inspector_json, reader_json, skeptic_json


def test_fenced_json_and_think_prefix():
    text = "<think>hmm {not json}</think>\nHere:\n```json\n" + json.dumps(reader_json()) + "\n```"
    assert parse(text, ReaderOutput).device_model == "ThinkPad T14 Gen 2"


def test_trailing_commas_python_literals():
    text = '{"images_reviewed": 1, "photo_kind": "real_device", "matches_title": "yes", "screen_state": "windows_login", "visible_damage": [], ' \
           '"asset_tag_or_inventory_sticker": False, "multiple_units_visible": True, "notes": "", "confidence": "high",}'
    out = parse(text, InspectorOutput)
    assert out.multiple_units_visible is True and out.asset_tag_or_inventory_sticker is False


def test_missing_field_filled_and_unknown_enum_mapped():
    data = reader_json()
    del data["seller_type"]
    del data["price_is_for_this_item"]
    data["working_status"] = "kinda works"
    data["scam_signals"] = ["paypal_friends", "made_up"]
    out = parse(json.dumps(data), ReaderOutput)
    assert out.seller_type == "unknown" and out.price_is_for_this_item is True
    assert out.working_status == "unknown" and out.scam_signals == ["paypal_friends"]


def test_garbage_raises_and_foreign_object_raises():
    with pytest.raises(ParseError):
        parse("I cannot help with that.", ReaderOutput)
    with pytest.raises(ParseError):
        parse('{"foo": 1}', ReaderOutput)


def test_nested_objections_and_broken_critic_defaults_to_watch():
    obj = {"kind": "scam", "claim": "c", "evidence": "e", "severity": "bogus"}
    out = parse(json.dumps(skeptic_json(objections=[obj], verdict="veto")), SkepticOutput)
    assert out.objections[0].kind == "scam" and out.objections[0].severity == "low"
    out = parse('{"objections": []}', SkepticOutput)
    assert out.verdict == "downgrade_to_watch"


def test_lists_truncated():
    out = parse(json.dumps(reader_json(questions_for_seller=["a", "b", "c", "d"], item_type_evidence=["x" * 300] * 9)), ReaderOutput)
    assert len(out.questions_for_seller) == 3 and len(out.item_type_evidence) == 5 and len(out.item_type_evidence[0]) == 120


def test_json_schema_is_strict_and_inlined():
    s = json_schema(SkepticOutput)
    assert "$defs" not in s and "$ref" not in json.dumps(s)
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    item = s["properties"]["objections"]["items"]
    assert item["additionalProperties"] is False and "title" not in item
    assert set(DEFAULTS[ReaderOutput]) == set(ReaderOutput.model_fields)
