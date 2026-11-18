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


def test_reader_prompt_treats_standalone_monitor_as_device():
    """Regression (live run 2026-11-17): GLM labelled 'Dell P2415Q 4K (23,8 Zoll) Monitor' an accessory because the
    prompt was laptop-only and listed Display/LCD as part words."""
    from huntbot.agents.prompts import READER
    from huntbot.agents.store import PROMPT_VERSION
    assert "monitor, display, TV, tablet or console sold" in READER and "on its own IS a device" in READER
    assert "(laptops, Dyson vacuums)" not in READER
    assert PROMPT_VERSION["reader"] != "reader-v1"   # cached v1 answers must not be reused
