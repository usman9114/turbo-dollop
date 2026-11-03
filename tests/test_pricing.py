import math

import pytest

from huntbot.config import Economics, Settings
from huntbot.db import DB
from huntbot.features import extract
from huntbot.models import Listing
from huntbot.pipeline import Pipeline
from huntbot.pricemodel import PRIOR, PriceModel, can_price, fit_from_db
from tests.test_core import make_screen
from huntbot import rules, scoring


@pytest.mark.parametrize("title,desc,expect", [
    ("Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB", "mit Netzteil",
     dict(line="thinkpad_t", year=2021, ram_gb=16, ssd_gb=512, condition="good", charger=True)),
    ("Lenovo Thinkpad E480 ,i5-8th Gen ,als defekt", "", dict(line="thinkpad_e", year=2018, condition="defect")),
    ("ThinkPad X1 Carbon 5th Gen i7-7500U 8GB RAM", "", dict(line="thinkpad_x1", year=2017, ram_gb=8)),   # model gen beats CPU gen
    ("Dell Latitude 5420", "i5-1145G7 16 GB RAM 256GB SSD neuwertig", dict(line="latitude_5", year=2021, condition="like_new")),
    ("Dell Latitude 5400", "", dict(line="latitude_5", year=2019)),
    ("MacBook Air M1 8/256GB", "", dict(line="macbook_air", year=2020, ram_gb=8, ssd_gb=256)),
    ("ThinkPad T460s", "Ohne Ladegerät, Gebrauchsspuren", dict(year=2016, charger=False, condition="worn")),
    ("ThinkPad T480", "nicht defekt, funktioniert einwandfrei", dict(condition="good")),
])
def test_feature_extraction(title, desc, expect):
    f = extract(title, desc).as_dict()
    for k, v in expect.items():
        assert f[k] == v, (k, f)


def test_priors_reproduce_plan_calibration():
    m = PriceModel()
    t480 = m.predict(extract("ThinkPad T480 i5-8350U 8GB 256GB SSD"))
    t14 = m.predict(extract("ThinkPad T14 Gen 2 i5-1135G7 16GB 256GB SSD"))
    assert 160 < t480.price < 215 and 280 < t14.price < 360        # plan: T480 €150–220, T14 G2 €250–350
    assert t480.low < t480.price < t480.high
    defect = m.predict(extract("ThinkPad T480 i5-8350U 8GB 256GB SSD defekt"))
    assert defect.price == pytest.approx(t480.price * math.exp(PRIOR["cond_defect"]), rel=1e-6)


def test_ridge_learns_from_data_and_shrinks_with_little():
    # The "market" says T-line laptops are 30% more expensive than the prior thinks.
    truth = PriceModel(coef=[PRIOR[n] + (0.26 if n == "line_thinkpad_t" else 0) for n in PRIOR])
    titles = [f"ThinkPad T{m} i5-{g}350U {r}GB {s}GB SSD" for m, g in (("480", 8), ("490", 8)) for r in (8, 16) for s in (256, 512)]
    others = [f"Dell Latitude 54{d}0 i5-{g}350U {r}GB 256GB SSD" for d, g in (("0", 8), ("1", 10)) for r in (8, 16)]
    rows = [(extract(t), truth.predict(extract(t)).price, 1.0) for t in titles + others]
    probe = extract("ThinkPad T480 i5-8350U 16GB 256GB SSD")
    prior_p, true_p = PriceModel().predict(probe).price, truth.predict(probe).price
    few = PriceModel.fit(rows[:2]).predict(probe).price
    many = PriceModel.fit(rows * 10).predict(probe).price
    assert prior_p < few <= true_p * 1.01             # moves toward the data even with 2 rows
    assert many == pytest.approx(true_p, rel=0.03)    # and converges on the market
    lat = extract("Dell Latitude 5400 i5-8350U 8GB 256GB SSD")   # other line stays where the market is
    assert PriceModel.fit(rows * 10).predict(lat).price == pytest.approx(truth.predict(lat).price, rel=0.03)


def test_json_roundtrip():
    m = PriceModel.fit([(extract("ThinkPad T480 8GB"), 200.0, 1.0)])
    m2 = PriceModel.from_json(m.to_json())
    f = extract("ThinkPad T480 8GB")
    assert m2.predict(f).price == pytest.approx(m.predict(f).price, rel=1e-9)


def test_can_price():
    assert can_price(extract("ThinkPad T480"))
    assert not can_price(extract("Samsung Galaxy S21"))
    assert not can_price(extract("Laptop zu verkaufen"))


def test_quick_flip_alert_for_underpriced_working_device(tmp_path):
    p = Pipeline(Settings(), DB(tmp_path / "f.db"))
    cheap = Listing(source="kleinanzeigen", ad_id="1", url="u", price=150.0, location="80331 Altstadt",
                    title="Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB SSD",
                    description="Funktioniert einwandfrei, mit Netzteil, Windows 11")
    r = p.process(cheap, use_llm=False)
    s = r.score
    assert s.mode == "flip" and s.alert and s.discount > 0.4 and s.hours == 0.75
    assert s.ev > 60 and s.max_buy > 150
    fair = Listing(**{**cheap.__dict__, "ad_id": "2", "price": 330.0, "description": "funktioniert, Netzteil dabei"})
    r2 = p.process(fair, use_llm=False)
    assert r2.score.mode == "flip" and not r2.score.alert


def test_flip_picks_better_platform():
    econ = Economics()
    screen = rules.screen(Listing(source="kleinanzeigen", ad_id="1", title="ThinkPad T480 i5-8350U 16GB", url="u"))
    l = Listing(source="kleinanzeigen", ad_id="1", title="ThinkPad T480 i5-8350U 16GB", url="u", price=90.0)
    s = scoring.score(l, screen, econ, PriceModel())
    v = scoring.valuation(l, screen, econ, PriceModel())
    nets = {p: scoring.net_of_sale(r, 90, econ, p) for p, r in v.resale_by_platform.items()}
    assert s.sell_on == max(nets, key=nets.get)


def test_comps_train_the_model(tmp_path):
    db = DB(tmp_path / "c.db")
    base = fit_from_db(db).predict(extract("ThinkPad T480 i5-8350U 8GB 256GB", platform="ebay", price_kind="sold")).price
    for _ in range(8):
        db.add_comp("ThinkPad T480 i5-8350U 8GB 256GB SSD", 260.0, "ebay", "sold")
    after = fit_from_db(db).predict(extract("ThinkPad T480 i5-8350U 8GB 256GB", platform="ebay", price_kind="sold")).price
    assert base < after < 262


# Regression cases taken from real Kleinanzeigen titles (2026-11-03 live test).
@pytest.mark.parametrize("title,is_part", [
    ("Lenovo Blickschutzfilter für ThinkPad P14s T14 T14s X1 von 3M", True),
    ("Lenovo ThinkPad T14 Gen 1 Tastatur – Deutsch QWERTZ", True),
    ('Lenovo ThinkPad T480 14" Display HD Panel Innolux', True),
    ("Tastatur thinkpad T480s", True),
    ("ThinkPad T480 Akku", True),
    ("ThinkPad T480 Tastatur defekt", False),
    ("ThinkPad T480 mit neuer Tastatur", False),
    ("Lenovo ThinkPad T460s Display defekt", False),
    ("Lenovo ThinkPad T480s silber i7/512 SSD/24 RAM mit Dockingstation", False),
    ("Laptop Lenovo ThinkPad T440 Laptop als Ersatzteile", False),
])
def test_real_part_titles(title, is_part):
    assert rules.is_accessory(title) is is_part


@pytest.mark.parametrize("title,desc,year,cond", [
    ("Lenovo Thinkpad T14 Gen. 5", "Ist original verpackt", 2024, "new"),
    ("Lenovo ThinkPad T14 Gen 6 – gut", "Intel Core Ultra 5 225U 16 GB DDR5", 2025, "good"),
    ("Lenovo ThinkPad T14 Gen 4 AMD *UNBENUTZT*", "", 2023, "new"),
    ("ThinkPad T14 G1 | Refurbished + Garantie", "", 2020, "like_new"),
    ("Lenovo ThinkPad X1 Carbon 7th Generation", "", 2019, "good"),
])
def test_real_generation_titles(title, desc, year, cond):
    f = extract(title, desc)
    assert (f.year, f.condition) == (year, cond)


def test_implausibly_cheap_flip_gets_no_buy_signal(tmp_path):
    p = Pipeline(Settings(), DB(tmp_path / "s.db"))
    r = p.process(Listing(source="kleinanzeigen", ad_id="9", url="u", price=40.0, location="80331",
                          title="Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB", description="funktioniert"), use_llm=False)
    assert not r.score.alert and r.score.reasons[0].startswith("CHECK")


def test_more_real_titles():
    assert not rules.is_accessory("⭕ Kompakter Lenovo Thinkpad T480 - i3 - SSD - Win 11 - Akku neu")
    l = Listing(source="kleinanzeigen", ad_id="1", url="u", title='Laptop Lenovo ThinkPad T480 i5,16 GB 500GB, 14" Full HD',
                description="Laptop springt nicht nur sporadisch an. Reparieren easy wer es kann.")
    assert rules.screen(l).fault_category == "mainboard"
    s = scoring.score(l, rules.screen(l), Economics(), PriceModel())
    assert s.mode == "repair" and s.buy_estimate > 0 and "assumed typical" in " ".join(s.reasons)


def test_stated_purchase_price_caps_estimate(tmp_path):
    """Real ad: unused T14 Gen 4 'Kaufpreis: 799,01 € (Februar 2026)', asking €700 VB → not a deal."""
    p = Pipeline(Settings(), DB(tmp_path / "r.db"))
    l = Listing(source="kleinanzeigen", ad_id="1", url="u", price=700.0, negotiable=True, shipping=True,
                location="81541 Au-Haidhausen", title="Lenovo ThinkPad T14 Gen 4 AMD *UNBENUTZT*",
                description="Ryzen 5 7540U * 16 GB RAM * 256 GB SSD * Kaufpreis: 799,01 € (Februar 2026) Rechnung")
    s = p.process(l, use_llm=False).score
    assert s.fair_ask <= 799 * 0.85 * 1.08 + 1 and s.resale <= 799 * 0.85 + 1
    assert not s.alert and s.max_buy < 595


def test_local_listing_is_picked_up_even_if_seller_ships():
    econ = Economics()
    screen = make_screen(fault_category="none")
    near = Listing(source="kleinanzeigen", ad_id="1", title="ThinkPad T480", url="u", price=100.0, location="81541", shipping=True)
    far = Listing(**{**near.__dict__, "location": "22041"})
    assert scoring.score(near, screen, econ, PriceModel()).cost_in < scoring.score(far, screen, econ, PriceModel()).cost_in


@pytest.mark.parametrize("text,price", [("Kaufpreis: 799,01 € (Februar 2026)", 799.01), ("NP 1.299€", 1299.0),
                                        ("Neupreis war 1.299,00 €", 1299.0), ("UVP: 449 EUR", 449.0), ("Preis 200 €", None)])
def test_retail_parse(text, price):
    from huntbot.features import retail_from_text
    assert retail_from_text(text) == price
