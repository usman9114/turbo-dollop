from pathlib import Path

import httpx2 as httpx
import pytest

from huntbot.config import Settings
from huntbot.db import DB
from huntbot.pipeline import Pipeline
from huntbot.sources.kleinanzeigen_live import USER_AGENT, LiveSearch, LiveSearchBlocked, parse_search_html

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.kleinanzeigen.de/s-muenchen/preis::150/thinkpad/k0l6411r30"


def make(tmp_path, handler, **kw):
    db = DB(tmp_path / "live.db")
    sleeps = []
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    return db, LiveSearch(db, http=http, sleep=sleeps.append, **kw), sleeps


def test_parse_search_fixture():
    ls = {l.ad_id: l for l in parse_search_html((FIX / "search_page.html").read_text())}
    assert set(ls) == {"2901234567", "2907654321", "2905555555"}
    t14 = ls["2901234567"]
    assert t14.price == 100 and t14.negotiable and t14.location == "80469 Isarvorstadt"
    assert t14.url == "https://www.kleinanzeigen.de/s-anzeige/lenovo-thinkpad-t14-gen-2-startet-nicht/2901234567"
    assert ls["2907654321"].shipping is True


def test_rate_limit_and_honest_user_agent(tmp_path):
    seen = []
    def handler(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(200, text=(FIX / "search_page.html").read_text())
    _, live, sleeps = make(tmp_path, handler)
    assert len(live.search(URL).listings) == 3
    live.search(URL)
    assert len(sleeps) == 1 and 29 < sleeps[0] <= 30           # waited before the second request
    assert all(ua.startswith("huntbot/") for ua in seen)


def test_block_pauses_24h(tmp_path):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(403, text="Forbidden")
    db, live, _ = make(tmp_path, handler)
    with pytest.raises(LiveSearchBlocked, match="paused for 24 h"):
        live.search(URL)
    with pytest.raises(LiveSearchBlocked, match="paused"):
        LiveSearch(db, http=live.http, sleep=lambda s: None).search(URL)   # survives restart
    assert len(calls) == 1                                                  # no retry hammering


def test_captcha_page_is_a_block(tmp_path):
    _, live, _ = make(tmp_path, lambda r: httpx.Response(200, text="<html>Bitte lösen Sie das CAPTCHA</html>"))
    with pytest.raises(LiveSearchBlocked, match="CAPTCHA"):
        live.search(URL)


def test_daily_cap_and_url_guard(tmp_path):
    _, live, _ = make(tmp_path, lambda r: httpx.Response(200, text="<html></html>"), daily_cap=1)
    live.search(URL)
    with pytest.raises(LiveSearchBlocked, match="daily cap"):
        live.search(URL)
    with pytest.raises(ValueError):
        live.search("https://www.kleinanzeigen.de/s-anzeige/x/123")


def test_hunt_uses_live_results(tmp_path):
    db, live, _ = make(tmp_path, lambda r: httpx.Response(200, text=(FIX / "search_page.html").read_text()))
    out = Pipeline(Settings(), db).hunt("thinkpad bis 150", live=live)
    assert out["live"][0]["count"] == 3
    assert out["results"][0].listing.ad_id == "2901234567" and out["results"][0].score.alert
    assert out["results"][-1].score.rejected


def test_parse_2026_layout():
    ls = {l.ad_id: l for l in parse_search_html((FIX / "search_page_2026.html").read_text())}
    t480, x390, dock = ls["3600000001"], ls["3600000002"], ls["3600000003"]
    assert t480.title.startswith("Lenovo ThinkPad T480") and (t480.price, t480.negotiable) == (60, True)
    assert t480.location == "80335 Maxvorstadt" and t480.shipping and t480.image_urls == ["https://img.example.test/t480.jpg"]
    assert "Kein BIOS-Passwort" in t480.description
    assert x390.title == "ThinkPad X390 Akku defekt" and x390.price == 50 and x390.image_urls == []   # no photo, <h3> title
    from huntbot.rules import screen
    assert "not_the_device" in screen(dock).red_flags


@pytest.mark.parametrize("title,acc", [
    ("Lenovo ThinkPad Thunderbolt 3 Dock Gen 2 mit 135W Netzteil", True),
    ("DICOTA Laptoptasche 13 / 14 Zoll NEU – z.B. für MacBook, ThinkPad", True),
    ("Netzteil für ThinkPad T480", True),
    ("✔️Lenovo Thinkpad Docstation 40 AJ m. Schlüssel und Ladegerät", True),
    ("ThinkPad T14 Gen 2 mit Netzteil", False),
    ("Dell Notebook mit Netzteil defekt", False),
    ("ThinkPad T480 Tastatur defekt", False),
    ("IBM ThinkPad T40", False),
])
def test_accessory_titles(title, acc):
    from huntbot.rules import is_accessory
    assert is_accessory(title) is acc
