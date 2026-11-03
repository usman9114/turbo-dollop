import math

import pytest

from huntbot.config import Economics, Settings
from huntbot.db import DB
from huntbot.features import extract
from huntbot.models import Listing
from huntbot.pipeline import Pipeline
from huntbot.pricemodel import PRIOR, PriceModel, can_price, fit_from_db
from huntbot import rules, scoring


@pytest.mark.parametrize("title,desc,expect", [
    ("Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB", "mit Netzteil",
     dict(line="thinkpad_t", year=2021, ram_gb=16, ssd_gb=512, condition="good", charger=True)),
    ("Lenovo Thinkpad E480 ,i5-8th Gen ,als defekt", "", dict(line="thinkpad_e", year=2018, condition="defect")),
    ("ThinkPad X1 Carbon 5th Gen i7-7500U 8GB RAM", "", dict(line="thinkpad_x1", year=2016, ram_gb=8)),
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
