"""Quotes that are technically verbatim but change the meaning must not verify (no human in the loop)."""
from huntbot.agents.ground import verified, verified_affirmative
from huntbot.models import Listing
from huntbot.rules import screen

TEXT = "Display nicht original, Akku defekt. Laptop funktioniert nicht mehr richtig. Kaufpreis: 799,01 €"


def test_ellipsis_cannot_stitch_short_words():
    assert not verified("nicht … defekt", TEXT)
    assert not verified("Display … Kaufpreis: 799,01 €", "Display " + "x" * 200 + " Kaufpreis: 799,01 €")  # gap too far
    assert verified("Display nicht original … Akku defekt", TEXT)


def test_truncated_positive_quote_is_rejected():
    assert verified("Laptop funktioniert", TEXT)                 # verbatim…
    assert not verified_affirmative("Laptop funktioniert", TEXT)  # …but it continues with "nicht"
    assert verified_affirmative("funktioniert einwandfrei", "Gerät funktioniert einwandfrei, mit Netzteil")


def test_rules_catch_plain_not_working():
    for d in ["Laptop funktioniert nicht", "nicht mehr funktionsfähig", "ungetestet, daher Bastler"]:
        assert screen(Listing(source="kleinanzeigen", ad_id="1", url="u", title="ThinkPad T480", description=d)).fault_category != "none"
