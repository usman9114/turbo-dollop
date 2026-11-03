from pathlib import Path

import pytest

from huntbot import catalog, query, rules, scoring
from huntbot.config import Economics, Settings
from huntbot.db import DB
from huntbot.models import Listing, Screen
from huntbot.offers import COPY_TEXT_LIMIT, draft_offer
from huntbot.pipeline import Pipeline
from huntbot.privacy import strip_pii
from huntbot.sources.ebay import parse_item
from huntbot.sources.kleinanzeigen import from_companion, parse_alert_email, parse_price

FIX = Path(__file__).parent / "fixtures"


def make_screen(**kw) -> Screen:
    base = dict(device_model="ThinkPad T14 Gen 2", catalog_key="thinkpad-t14-g2", fault_summary="dead SSD",
                fault_category="storage", p_ok=1.0, parts_cost_eur=37.82, repair_hours=1.5, red_flags=[],
                confidence="high", questions_for_seller=[])
    base.update(kw)
    return Screen(**base)


# ---------------------------------------------------------------- scoring
def test_plan_section6_worked_example(monkeypatch):
    """T14 G2 bought €90, sold €265 on eBay, SSD €37.82 net, 20 km travel -> profit €70.20 (plan §6)."""
    entry = catalog.by_key("thinkpad-t14-g2")
    monkeypatch.setattr(catalog, "by_key", lambda k: entry.__class__(**{**entry.__dict__, "resale_p50": 265.0, "salvage": 0.0}))
    econ = Economics(p_ok_cap=1.0)
    listing = Listing(source="kleinanzeigen", ad_id="1", title="T14", url="u", price=90.0)
    s = scoring.score(listing, make_screen(), econ)
    assert s.ev == pytest.approx(70.20, abs=0.01)
    assert s.ev_per_hour == pytest.approx(46.8, abs=0.1)
    # Kleinunternehmer variant: +€27.94 VAT, but SSD would be gross (€45) -> plan says €90.96
    s2 = scoring.score(listing, make_screen(parts_cost_eur=45.0), Economics(p_ok_cap=1.0, kleinunternehmer=True))
    assert s2.ev == pytest.approx(90.96, abs=0.01)


def test_reject_flags_and_multipliers():
    econ = Economics()
    l = Listing(source="kleinanzeigen", ad_id="1", title="T14", url="u", price=50.0)
    assert scoring.score(l, make_screen(red_flags=["activation_lock"]), econ).rejected
    clean = scoring.score(l, make_screen(), econ).ev
    bios = scoring.score(l, make_screen(red_flags=["bios_password"]), econ).ev
    assert bios == pytest.approx(clean * 0.5)


def test_max_buy_hits_threshold():
    econ = Economics()
    l = Listing(source="kleinanzeigen", ad_id="1", title="T14", url="u", price=100.0)
    screen = make_screen(p_ok=0.8)
    mb = scoring.max_buy_price(l, screen, econ)
    at = Listing(**{**l.__dict__, "price": mb})
    above = Listing(**{**l.__dict__, "price": mb + 2})
    assert scoring.score(at, screen, econ).ev >= econ.min_ev
    assert scoring.score(above, screen, econ).ev < econ.min_ev


def test_unknown_model_is_not_alerted():
    s = scoring.score(Listing(source="ebay", ad_id="1", title="x", url="u", price=10.0),
                      make_screen(catalog_key="none"), Economics())
    assert not s.alert and "unknown model" in s.reasons[0]


# ---------------------------------------------------------------- catalog / rules
@pytest.mark.parametrize("title,key", [
    ("Lenovo ThinkPad T14 Gen 2 i5-1135G7", "thinkpad-t14-g2"),
    ("Thinkpad T14 G3 defekt", "thinkpad-t14-g3"),
    ("ThinkPad T14 i5-10310U", "thinkpad-t14-g1"),
    ("ThinkPad T14", "thinkpad-t14"),
    ("Lenovo T480s Bastler", "thinkpad-t480"),
    ("Dell Latitude 5420 Display", "latitude-5420"),
    ("HP EliteBook 840 G7", "elitebook-840-g7"),
    ("Dyson V11 Absolute Akku defekt", "dyson-v11"),
    ("Samsung Galaxy S21", None),
])
def test_catalog_match(title, key):
    entry = catalog.match(title)
    assert (entry.key if entry else None) == key


def test_rules_flags_and_faults():
    l = Listing(source="kleinanzeigen", ad_id="1", title="ThinkPad T480 BIOS Passwort vergessen",
                url="u", description="Firmenlaptop mit Inventaraufkleber, ohne Netzteil. Muss heute weg!")
    s = rules.screen(l)
    assert {"bios_password", "company_asset_tag", "no_charger", "suspicious_urgency"} <= set(s.red_flags)
    ok = rules.screen(Listing(source="kleinanzeigen", ad_id="2", title="T480", url="u",
                              description="Kein BIOS-Passwort, SSD defekt"))
    assert "bios_password" not in ok.red_flags and ok.fault_category == "storage"


def test_rules_merge_keeps_rule_flags():
    merged = rules.merge(make_screen(red_flags=["activation_lock"]), make_screen(red_flags=["no_charger"]))
    assert set(merged.red_flags) == {"activation_lock", "no_charger"}


# ---------------------------------------------------------------- query
def test_query_parse():
    hq = query.parse("thinkpad t14 defekt bis 150€ versand")
    assert hq.keywords == ["thinkpad", "t14", "defekt"]
    assert hq.max_price == 150 and hq.shipping_only and hq.broken_only
    assert query.fts_query(hq) == '"thinkpad"* AND "t14"*'
    hq2 = query.parse("latitude 5420 50-120 €")
    assert (hq2.min_price, hq2.max_price) == (50, 120)
    assert any("defekt" in v for v in hq2.variants)


def test_kleinanzeigen_urls():
    hq = query.parse("thinkpad t14 bis 150")
    assert query.kleinanzeigen_url("thinkpad t14", hq, "muenchen", "6411", 30) == \
        "https://www.kleinanzeigen.de/s-muenchen/preis::150/thinkpad-t14/k0l6411r30"
    assert query.kleinanzeigen_url("thinkpad t14", query.parse("thinkpad t14")) == \
        "https://www.kleinanzeigen.de/s-thinkpad-t14/k0"
    assert "conditionIds:{7000}" in query.ebay_filter(hq) and "price:[0..150]" in query.ebay_filter(hq)


# ---------------------------------------------------------------- sources
@pytest.mark.parametrize("text,expected", [
    ("150 € VB", (150.0, True)), ("1.250 €", (1250.0, False)), ("Zu verschenken", (0.0, False)),
    ("VB", (None, True)), ("99,50 €", (99.5, False)),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_alert_email_parser():
    listings = {l.ad_id: l for l in parse_alert_email((FIX / "alert_email.eml").read_bytes())}
    assert set(listings) == {"2911111111", "2922222222"}
    x390 = listings["2911111111"]
    assert x390.title == "ThinkPad X390 Akku defekt" and x390.price == 75 and x390.negotiable
    assert x390.location.startswith("80333") and x390.image_urls == ["https://img.example.test/x390.jpg"]
    assert x390.url == "https://www.kleinanzeigen.de/s-anzeige/thinkpad-x390-akku-defekt/2911111111"
    assert listings["2922222222"].price == 0.0  # free, found inside a tracking redirect


def test_companion_payload_strips_pii():
    l = from_companion({"url": "https://www.kleinanzeigen.de/s-anzeige/t480/2900000001-278-6411",
                        "title": "T480", "price_text": "80 €", "description": "Ruf an: 0171 2345678, max@web.de",
                        "location": "80331 Altstadt", "seller": "Max", "images": ["http://insecure/x.jpg"]})
    assert l.ad_id == "2900000001" and l.price == 80
    assert "0171" not in l.description and "max@web.de" not in l.description
    assert l.seller_hash and l.seller_hash != "Max" and l.image_urls == []
    assert from_companion({"url": "https://evil.test/", "ad_id": "abc"}) is None


def test_ebay_parse_item():
    l = parse_item({"itemId": "v1|123|0", "title": "ThinkPad T490 defekt", "price": {"value": "55.00", "currency": "EUR"},
                    "itemWebUrl": "https://www.ebay.de/itm/123", "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
                    "shippingOptions": [{"shippingCost": {"value": "6.99"}}], "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
                    "condition": "Als Ersatzteil / defekt", "itemLocation": {"postalCode": "80***", "country": "DE"},
                    "seller": {"username": "someone"}})
    assert l.price == 55 and l.shipping_cost == 6.99 and l.negotiable and l.key == "ebay:v1|123|0"


def test_strip_pii():
    assert strip_pii("Tel +49 89 1234567 oder Hauptstraße 12") == "Tel [telefon] oder [adresse]"


# ---------------------------------------------------------------- offers
def test_offer_fits_copy_text_limit():
    l = Listing(source="kleinanzeigen", ad_id="1", title="x" * 300, url="u", price=150.0, negotiable=True)
    s = make_screen(p_ok=0.85, questions_for_seller=["Ist das Display original und ohne Pixelfehler, und gibt es eine Rechnung?"])
    sc = scoring.score(l, s, Economics())
    offer = draft_offer(l, s, sc)
    assert 0 < len(offer) <= COPY_TEXT_LIMIT and "€" in offer and "BIOS" in offer


# ---------------------------------------------------------------- pipeline
class FakeScreener:
    screen_model, review_model = "fake-haiku", "fake-sonnet"

    def __init__(self, result: Screen, review: Screen | None = None):
        self.result, self.review_result, self.calls = result, review, []

    def screen(self, listing):
        self.calls.append(("screen", listing.key))
        return self.result

    def review(self, listing):
        self.calls.append(("review", listing.key))
        return self.review_result


class FakeEbay:
    def __init__(self, listings):
        self.listings, self.queries = listings, []

    def search(self, q, parts_only=True):
        self.queries.append(q.text)
        return self.listings


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_pipeline_escalates_and_notifies_once(db):
    notified = []
    screener = FakeScreener(make_screen(p_ok=0.85, confidence="medium"), review=make_screen(p_ok=0.8, confidence="high"))
    p = Pipeline(Settings(), db, screener=screener, notifier=notified.append)
    l = Listing(source="kleinanzeigen", ad_id="42", title="ThinkPad T14 Gen 2 SSD defekt", url="u", price=80.0)
    r = p.process(l)
    assert r.model_used == "fake-sonnet" and r.score.alert
    assert [c[0] for c in screener.calls] == ["screen", "review"]
    p.process(Listing(**{**l.__dict__}))  # same listing again: cached, no new calls, no 2nd notification
    assert len(screener.calls) == 2 and len(notified) == 1


def test_pipeline_rejects_without_llm_call(db):
    screener = FakeScreener(make_screen())
    p = Pipeline(Settings(), db, screener=screener)
    r = p.process(Listing(source="kleinanzeigen", ad_id="7", title="ThinkPad T480 iCloud gesperrt", url="u", price=30.0))
    assert r.score.rejected and screener.calls == []


def test_hunt_merges_ebay_and_local_and_ranks(db):
    p = Pipeline(Settings(), db)  # no LLM
    p.process(Listing(source="kleinanzeigen", ad_id="1", title="ThinkPad T14 Gen 2 Passwort vergessen", url="u1", price=90.0))
    p.process(Listing(source="kleinanzeigen", ad_id="2", title="Dyson V11 defekt", url="u2", price=40.0))
    ebay = FakeEbay([Listing(source="ebay", ad_id="e1", title="ThinkPad T14 Gen 2 defekt", url="e", price=200.0, shipping_cost=6.99)])
    out = p.hunt("thinkpad t14 bis 250", ebay=ebay)
    keys = [r.listing.key for r in out["results"]]
    assert keys == ["kleinanzeigen:1", "ebay:e1"]          # Dyson filtered out, cheaper deal ranked first
    assert out["results"][0].score.ev > out["results"][1].score.ev
    assert "k0l6411r30" in out["kleinanzeigen_links"][0]["url"]


def test_repost_with_same_text_is_not_new(db):
    p = Pipeline(Settings(), db)
    a = p.process(Listing(source="kleinanzeigen", ad_id="100", title="T480 defekt", url="u", price=50.0, description="Akku tot"))
    b = p.process(Listing(source="kleinanzeigen", ad_id="101", title="T480 defekt", url="u", price=50.0, description="Akku tot"))
    assert a.is_new and not b.is_new


def test_purge_keeps_bought(db):
    p = Pipeline(Settings(), db)
    p.process(Listing(source="kleinanzeigen", ad_id="1", title="T480", url="u", price=50.0))
    p.process(Listing(source="kleinanzeigen", ad_id="2", title="T490", url="u", price=50.0))
    db.add_purchase("T490", 50.0, "kleinanzeigen:2")
    db.conn.execute("UPDATE listings SET last_seen = 0")
    assert db.purge(60) == 1 and db.get_listing("kleinanzeigen:2")


def test_hunt_llm_budget_limits_claude_calls(db):
    screener = FakeScreener(make_screen(p_ok=0.85, confidence="high"))
    p = Pipeline(Settings(), db, screener=screener)
    listings = [Listing(source="ebay", ad_id=str(i), title=f"ThinkPad T14 Gen 2 defekt #{i}", url="u", price=60.0 + i * 20)
                for i in range(5)]
    out = p.hunt("thinkpad t14", ebay=FakeEbay(listings), llm_budget=2)
    screened = [c[1] for c in screener.calls if c[0] == "screen"]
    assert len(screened) == 2 and set(screened) == {"ebay:0", "ebay:1"}   # cheapest = highest rule EV
    assert sum(r.model_used != "rules" for r in out["results"]) == 2


def test_unknown_model_with_hard_flag_is_rejected():
    s = scoring.score(Listing(source="kleinanzeigen", ad_id="1", title="Dock", url="u", price=70.0),
                      make_screen(catalog_key="none", red_flags=["not_the_device"]), Economics())
    assert s.rejected and s.reasons[0].startswith("REJECT")


def test_far_away_listing_uses_shipping_or_is_not_alerted():
    econ = Economics()
    screen = make_screen(p_ok=0.85)
    near = Listing(source="kleinanzeigen", ad_id="1", title="T14", url="u", price=60.0, location="80331 Altstadt")
    far = Listing(**{**near.__dict__, "location": "22041 Wandsbek"})
    far_ship = Listing(**{**far.__dict__, "shipping": True})
    assert scoring.score(near, screen, econ).alert
    s_far = scoring.score(far, screen, econ)
    assert not s_far.alert and "ask seller to ship" in s_far.reasons[-1]
    s_ship = scoring.score(far_ship, screen, econ)
    assert s_ship.alert and s_ship.cost_in == pytest.approx(60 + 60 * 0.045 + 0.5 + 37.82 + 6.99)
