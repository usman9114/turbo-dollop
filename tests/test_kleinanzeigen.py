from pathlib import Path

from hunter import kleinanzeigen as ka
from hunter.email_alerts import parse_alert_html

FIX = Path(__file__).parent / "fixtures"


def test_parse_price():
    assert ka.parse_price("60 € VB") == (60.0, True, False)
    assert ka.parse_price("1.200 €") == (1200.0, False, False)
    assert ka.parse_price("49,99 €") == (49.99, False, False)
    assert ka.parse_price("VB") == (None, True, False)
    assert ka.parse_price("Zu verschenken") == (0.0, False, True)
    assert ka.parse_price("12") is None
    assert ka.parse_price("ThinkPad 14 €uro") is None


def test_parse_search_cards():
    items = ka.parse_search((FIX / "ka_search.html").read_text())
    assert [x.ad_id for x in items] == ["1000000001", "1000000002", "1000000003"]
    a, b, c = items
    assert a.title.startswith("Lenovo ThinkPad T14 Gen 2")
    assert (a.price_eur, a.negotiable, a.postal_code, a.distance_km) == (90.0, True, "80335", 2.0)
    assert a.posted == "Heute, 09:12" and a.shipping_possible and a.photo_count == 6
    assert a.url == "https://www.kleinanzeigen.de/s-anzeige/lenovo-thinkpad-t14-gen-2-passwort-vergessen/1000000001-278-1"
    assert a.image_urls[0].endswith("?rule=$_59.AUTO")
    assert b.price_eur == 1200.0 and b.direct_buy  # first price wins over the struck-out one
    assert c.free and c.price_eur == 0.0 and c.distance_km == 412.0 and not c.shipping_possible


def test_parse_ad_page():
    ad = ka.parse_ad((FIX / "ka_ad.html").read_text(), "https://www.kleinanzeigen.de/s-anzeige/x/1000000001-278-1")
    assert ad.ad_id == "1000000001" and ad.detailed
    assert ad.title == "Lenovo ThinkPad T14 Gen 2 i5 16GB - Passwort vergessen"
    assert ad.status == "reserved"  # visible "Reserviert" badge
    assert ad.attributes == {"Marke": "Lenovo", "Zustand": "Defekt"}
    assert ad.description == "Startet, Windows Passwort vergessen.\nNetzteil dabei."
    assert len(ad.image_urls) == 2 and ad.seller_type == "private" and ad.postal_code == "80335"


def test_urls():
    url = ka.search_url("thinkpad defekt", "80331", 50, max_price=150, category="notebooks")
    assert "keywords=thinkpad+defekt" in url and "categoryId=278" in url and "maxPrice=150" in url
    assert ka.page_url("https://www.kleinanzeigen.de/s-80331/thinkpad-defekt/k0l6443r50", 3) == \
        "https://www.kleinanzeigen.de/s-80331/seite:3/thinkpad-defekt/k0l6443r50"
    assert ka.page_url("https://www.kleinanzeigen.de/s-80331/preis::150/thinkpad/k0l6443r50", 2) == \
        "https://www.kleinanzeigen.de/s-80331/preis::150/seite:2/thinkpad/k0l6443r50"


def test_blocked_detection():
    assert ka.looks_blocked("<html>Bitte löse das CAPTCHA</html>")
    assert not ka.looks_blocked((FIX / "ka_search.html").read_text())


def test_alert_email():
    items = {x.ad_id: x for x in parse_alert_html((FIX / "alert_email.html").read_text())}
    assert set(items) == {"1000000011", "1000000012"}
    t490 = items["1000000011"]
    assert t490.title == "ThinkPad T490 Akku defekt"
    assert (t490.price_eur, t490.negotiable, t490.postal_code) == (75.0, True, "80469")
    assert t490.url.endswith("/s-anzeige/thinkpad-t490-akku-defekt/1000000011-278-1")
    assert items["1000000012"].price_eur == 120.0
