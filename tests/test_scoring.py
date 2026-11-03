import pytest

from hunter import config
from hunter.assessment import Assessment, rules_assessment
from hunter.models import Listing
from hunter.prices import PriceTable
from hunter.scoring import score


@pytest.fixture
def s(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = config.load()
    st.radius_km = 50
    st.vat_mode = "25a"
    return st


@pytest.fixture
def prices():
    return PriceTable.load()


def _assessment(**kw):
    base = dict(relevant=True, model_key="thinkpad_t14_g2", model_guess="T14 G2", fault_summary="dead SSD",
                likely_fix="new SSD", parts_cost_eur=45, repair_hours=1.0, p_ok=0.9, red_flags=[],
                confidence=0.9, notes="")
    base.update(kw)
    return Assessment(**base)


def test_model_matching(prices):
    assert prices.match("Lenovo ThinkPad T14s Gen 2 - i5 11. Gen").key == "thinkpad_t14_g2"
    assert prices.match("ThinkPad T14 i5-1245U 12. Gen").key == "thinkpad_t14_g3"
    assert prices.match("ThinkPad T14 i5-10210U").key == "thinkpad_t14_g1"
    assert prices.match("Dell Latitude 5420 defekt").key == "latitude_5420"
    assert prices.match("HP EliteBook 840 G8").key == "elitebook_840_g8"
    assert prices.match("X1 Carbon 7th Generation").key == "thinkpad_x1c_g7"
    assert prices.match("Acer Aspire 5") is None


def test_plan_example_t14_pickup(s, prices):
    """Plan §6: T14 G2 bought for €90 nearby, SSD fix, resale ~€290 -> a clear deal."""
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=90, distance_km=10)
    sc = score(l, _assessment(), prices, s)
    assert sc.pickup and sc.breakdown["travel_or_ship_in"] == pytest.approx(6.0)
    assert sc.breakdown["vat_25a"] == pytest.approx((290 - 90) * 19 / 119, abs=0.01)
    assert sc.ev > 60 and sc.alert
    assert sc.max_buy > 90


def test_kleinunternehmer_has_no_vat(s, prices):
    s.vat_mode = "kleinunternehmer"
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=90, distance_km=10)
    assert score(l, _assessment(), prices, s).breakdown["vat_25a"] == 0


def test_far_listing_is_shipped_with_buyer_fee(s, prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=100, distance_km=300,
                shipping_possible=True)
    sc = score(l, _assessment(), prices, s)
    assert not sc.pickup
    assert sc.breakdown["buy_fee"] == pytest.approx(100 * 0.045 + 0.5)
    assert sc.breakdown["travel_or_ship_in"] == pytest.approx(s.ship_in_eur)


def test_vb_discount_and_overpriced(s, prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=280, negotiable=True, distance_km=5)
    sc = score(l, _assessment(), prices, s)
    assert sc.buy_assumed == pytest.approx(252)
    assert not sc.alert and sc.ev < 0


def test_hard_red_flags_reject(s, prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=50, distance_km=5)
    sc = score(l, _assessment(red_flags=["mdm_or_autopilot"]), prices, s)
    assert sc.rejected == "mdm_or_autopilot" and not sc.alert


def test_bios_password_halves_ev(s, prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=60, distance_km=5)
    clean = score(l, _assessment(), prices, s)
    locked = score(l, _assessment(red_flags=["bios_password"]), prices, s)
    assert locked.ev == pytest.approx(clean.ev * 0.5, abs=0.01)


def test_unknown_price_gives_max_buy(s, prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T14 Gen 2", price_eur=None, negotiable=True, distance_km=5)
    sc = score(l, _assessment(), prices, s)
    assert sc.ev is None and sc.max_buy and sc.max_buy > 0


def test_rules_assessment_flags(prices):
    l = Listing("kleinanzeigen", "1", "u", "ThinkPad T480 BIOS Passwort gesperrt",
                description="Firmengerät mit Inventar Aufkleber, ohne Netzteil")
    a = rules_assessment(l, prices)
    assert a.model_key == "thinkpad_t480"
    assert {"bios_password", "company_asset_tag", "no_charger"} <= set(a.red_flags)
    l2 = Listing("kleinanzeigen", "2", "u", "MacBook iCloud gesperrt")
    assert "activation_lock" in rules_assessment(l2, prices).red_flags


def test_part_only_listings(prices):
    from hunter.assessment import looks_like_part_only

    def L(title, **kw):
        return Listing("kleinanzeigen", "1", "u", title, **kw)

    assert looks_like_part_only(L("Original Lenovo Thinkpad Tastatur T14 defekt"))
    assert looks_like_part_only(L("Akku für ThinkPad T480"))
    assert not looks_like_part_only(L("ThinkPad T480 Akku defekt"))
    assert not looks_like_part_only(L("Lenovo Laptop T14 Tastatur defekt"))
    assert looks_like_part_only(L("ThinkPad T14", attributes={"Typ": "Zubehör"}))
    assert "not_a_device" in rules_assessment(L("Original Lenovo Thinkpad Tastatur T14 defekt"), prices).red_flags


def test_sold_title_and_reposts():
    from hunter.kleinanzeigen import status_from_title
    from hunter.pipeline import drop_reposts

    assert status_from_title("Laptop Lenovo ThinkPad T14 Gen1,Bastler *VERKAUFT !!!!!!") == "sold"
    assert status_from_title("ThinkPad T480 - RESERVIERT") == "reserved"
    assert status_from_title("ThinkPad T480 wird verkauft") == "active"
    a = Listing("kleinanzeigen", "1", "u", "Lenovo ThinkPad T14s Gen 2 - i5 11. Gen - 16GB RAM - M.2 Defekt",
                price_eur=199, postal_code="79541")
    b = Listing("kleinanzeigen", "2", "u", "Lenovo ThinkPad T14s Gen 2 - 15 11. Gen - 16GB RAM - M.2 Defekt",
                price_eur=199, postal_code="79541")
    c = Listing("kleinanzeigen", "3", "u", "Lenovo ThinkPad T14s Gen 2", price_eur=199, postal_code="10115")
    assert [x.ad_id for x in drop_reposts([a, b, c])] == ["1", "3"]


def test_part_title_beats_seller_category():
    from hunter.assessment import looks_like_part_only

    keyboard = Listing("kleinanzeigen", "1", "u", "Original Lenovo Thinkpad Tastatur T14 defekt",
                       attributes={"Typ": "Laptop"})
    assert looks_like_part_only(keyboard)
