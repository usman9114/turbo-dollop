"""Claude calls, checked against a fake HTTP transport (no API key or network needed)."""

import json

import anthropic
import httpx2 as httpx
import pytest

from hunter import screen
from hunter.models import Listing
from hunter.prices import PriceTable

ASSESSMENT = {
    "relevant": True, "model_key": "thinkpad_t14_g2", "model_guess": "ThinkPad T14 Gen 2",
    "fault_summary": "forgotten Windows password", "likely_fix": "reinstall Windows 11",
    "parts_cost_eur": 0, "repair_hours": 0.7, "p_ok": 0.97, "red_flags": ["no_charger"],
    "confidence": 0.8, "notes": "ask for BIOS photo",
}


@pytest.fixture
def captured(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append({"path": request.url.path, "headers": dict(request.headers), "body": body})
        schema_props = body["output_config"]["format"]["schema"]["properties"]
        out = dict(ASSESSMENT)
        if "offer_eur" in schema_props:
            out |= {"offer_eur": 80, "offer_message_de": "Hallo, ich würde 80 € bieten. Viele Grüße"}
        if "kleinanzeigen_queries" in schema_props:
            out = {"summary": "T14 to repair", "kleinanzeigen_queries": ["thinkpad t14 defekt", "t14 passwort"],
                   "ebay_query": "thinkpad t14", "category": "notebooks", "max_price": 150, "exclude_words": ["suche"]}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(out)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    fake = anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(screen, "_client", fake)
    return requests


LISTING = Listing("kleinanzeigen", "1", "https://www.kleinanzeigen.de/s-anzeige/x/1-278-1",
                  "ThinkPad T14 Gen 2 Passwort vergessen", description="Windows Passwort vergessen",
                  price_eur=90, negotiable=True, image_urls=[f"https://img.example/{i}.jpg" for i in range(6)])


def test_screen_request_and_parse(captured):
    a = screen.screen(LISTING, "thinkpad", PriceTable.load(), "claude-haiku-5-5")
    assert a.model_key == "thinkpad_t14_g2" and a.red_flags == ["no_charger"]
    req = captured[0]["body"]
    assert req["model"] == "claude-haiku-5-5" and req["output_config"]["effort"] == "low"
    images = [b for b in req["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 4 and images[0]["source"]["type"] == "url"
    assert "thinkpad_t14_g2" in req["system"][0]["text"]  # price catalogue in the prompt


def test_deep_review_uses_fallbacks(captured):
    r = screen.deep_review(LISTING, "thinkpad", PriceTable.load(), "claude-sonnet-5-5", max_buy=120)
    assert r.offer_eur == 80 and "Viele Grüße" in r.offer_message_de
    req = captured[0]
    assert req["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"]["anthropic-beta"]
    assert "€120" in req["body"]["messages"][0]["content"][-1]["text"]


def test_plan_query(captured):
    p = screen.plan_query("thinkpad t14 to fix under 150", "claude-haiku-5-5")
    assert p.kleinanzeigen_queries == ["thinkpad t14 defekt", "t14 passwort"] and p.max_price == 150


def test_pipeline_with_claude(captured, tmp_path, monkeypatch):
    from hunter import config
    from hunter.pipeline import Hunter
    from hunter.store import Store

    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "db.sqlite3"))
    s = config.load()
    s.use_claude = True
    st = Store(s.db_path)
    listing = Listing(**{**LISTING.to_dict(), "distance_km": 5.0})
    hits = Hunter(s, st, PriceTable.load(), log=lambda *_: None).evaluate([listing], "thinkpad", deep=1)
    h = hits[0]
    assert h.screened_by == "claude-sonnet-5-5"  # escalated to the deep review
    assert h.score.alert and h.score.ev > 40
    assert h.assessment.p_ok == 0.9  # capped until calibrated
    # second run hits the screening cache: only the deep review calls the API again
    n = len(captured)
    Hunter(s, st, PriceTable.load(), log=lambda *_: None).evaluate([listing], "thinkpad", deep=0)
    assert len(captured) == n
