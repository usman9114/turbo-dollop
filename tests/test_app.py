import pytest
from fastapi.testclient import TestClient

from huntbot import reports
from huntbot.config import Economics, Settings
from huntbot.db import DB
from huntbot.models import Listing
from huntbot.pipeline import Pipeline
from huntbot.telegram import Bot, card
from huntbot.web import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(token="secret")
    db = DB(tmp_path / "t.db")
    pipeline = Pipeline(settings, db)
    return settings, db, pipeline, TestClient(create_app(settings, db, pipeline))


ITEM = {"url": "https://www.kleinanzeigen.de/s-anzeige/thinkpad-t14-gen-2/2901234567-278-6411",
        "title": "Lenovo ThinkPad T14 Gen 2 Windows Fehler", "price_text": "100 € VB",
        "description": "Windows startet nicht", "location": "80469 Isarvorstadt", "images": []}


def test_ingest_requires_token(env):
    *_, client = env
    assert client.post("/api/ingest", json={"items": [ITEM]}).status_code == 401


def test_ingest_then_hunt(env):
    *_, client = env
    h = {"X-Huntbot-Token": "secret"}
    res = client.post("/api/ingest", json={"page": "search", "items": [ITEM, {"url": "bogus"}]}, headers=h).json()
    badge = res["results"]["2901234567"]
    assert badge["model"] == "ThinkPad T14 Gen 2" and badge["ev"] > 40 and badge["alert"]
    hunt = client.get("/api/hunt", params={"q": "t14 bis 150"}, headers=h).json()
    assert hunt["results"][0]["key"] == "kleinanzeigen:2901234567" and hunt["results"][0]["offer"]
    assert client.get("/").status_code == 200


def test_ledger_and_stats(tmp_path):
    db = DB(tmp_path / "l.db")
    inv = db.add_purchase("T14 G2", 90.0)
    db.add_cost(inv, "parts", 37.82)
    db.add_cost(inv, "parts", 6.0, "travel")
    db.add_cost(inv, "labour_min", 90)
    db.record_sale(inv, 265.0, fees=13.25, shipping=9.19)
    row = reports.ledger(db, Economics())[0]
    assert row["profit"] == pytest.approx(70.20, abs=0.01) and row["per_hour"] == "€47/h"
    assert "1/30 sales" in reports.summary_text(db, Economics())
    assert reports.ledger_csv(db, Economics()).startswith("id,title")


class FakeTG:
    chat_id = "1"

    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, text, **kw):
        self.sent.append(text)

    def send_card(self, result):
        self.sent.append(("card", result.listing.key))

    def call(self, method, **kw):
        self.calls.append(method)


def test_telegram_card_and_commands(env):
    settings, db, pipeline, _ = env
    r = pipeline.process(Listing(source="kleinanzeigen", ad_id="5", title="ThinkPad T14 Gen 2 SSD defekt",
                                 url="https://www.kleinanzeigen.de/s-anzeige/x/5", price=90.0))
    text, markup = card(r)
    assert "EV" in text and markup["inline_keyboard"][0][0]["url"].startswith("https://")
    assert any("copy_text" in b for b in markup["inline_keyboard"][1])

    tg = FakeTG()
    bot = Bot(tg, pipeline, db)
    msg = lambda t, chat="1": {"update_id": 1, "message": {"chat": {"id": chat}, "text": t}}
    bot.handle(msg("thinkpad t14"))                      # plain text = hunt
    assert ("card", "kleinanzeigen:5") in tg.sent
    bot.handle(msg("/bought kleinanzeigen:5 85"))
    bot.handle(msg("/cost 1 45 ssd"))
    bot.handle(msg("/time 1 60"))
    bot.handle(msg("/sold 1 260"))
    assert db.status("kleinanzeigen:5") == "sold"
    assert any("profit" in s for s in tg.sent if isinstance(s, str))
    n = len(tg.sent)
    bot.handle(msg("/stats", chat="999"))                # other chats are ignored
    assert len(tg.sent) == n
