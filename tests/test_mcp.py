import asyncio
import json

import pytest

pytest.importorskip("mcp")

from huntbot.config import Settings
from huntbot.db import DB
from huntbot.mcp_server import build


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    return result


def _payload(result):
    """CallToolResult → the tool's return value (structured output, unwrapped)."""
    assert not result.is_error, result.content
    data = result.structured_content
    if data is None:  # dict-returning tools come back as one JSON text block
        return json.loads("".join(b.text for b in result.content))
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


@pytest.fixture
def server(tmp_path):
    return build(Settings(), DB(tmp_path / "m.db"))


def test_tools_listed(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"hunt", "estimate_price", "score_listing", "top_deals", "add_sold_price"} <= names


def test_score_then_hunt_then_top(server):
    scored = _payload(call(server, "score_listing", {
        "title": "Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB SSD", "price": 150,
        "description": "funktioniert einwandfrei, mit Netzteil", "location": "80331 Altstadt"}))
    assert scored["mode"] == "flip" and scored["buy_signal"] and scored["offer_draft"]
    found = _payload(call(server, "hunt", {"query": "thinkpad t14"}))
    assert found["results"][0]["title"].startswith("Lenovo ThinkPad T14")
    assert found["kleinanzeigen_search_links"][0]["url"].startswith("https://www.kleinanzeigen.de/")
    assert _payload(call(server, "top_deals", {}))[0]["buy_signal"] is True


def test_estimate_and_comp(server):
    est = _payload(call(server, "estimate_price", {"title": "ThinkPad T480 i5-8350U 16GB", "asking_price": 120}))
    assert est["estimates_eur"]["ebay_sold"]["price"] > 120
    msg = _payload(call(server, "add_sold_price", {"title": "ThinkPad T480 i5-8350U 16GB", "price": 230}))
    assert "retrained" in str(msg)
