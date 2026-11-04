from huntbot import rules, scoring
from huntbot.config import Economics, Settings, load_settings
from huntbot.models import Listing


def L(title, desc="", price=100.0):
    return Listing("kleinanzeigen", "7", title, "u", price, False, desc, "80331 München")


def test_scam_pattern_is_rejected():
    l = L("Lenovo ThinkPad T14 Gen 2", "Bezahlung nur per PayPal Freunde, Versand.")
    assert "scam_pattern" in rules.screen(l).red_flags
    assert scoring.score(l, rules.screen(l), Economics()).rejected
    # "Nur PayPal" alone is not the pattern (the Reader's paypal_only handles it)
    assert "scam_pattern" not in rules.red_flags(L("Lenovo ThinkPad T14", "Zahlung nur PayPal oder bar."))


def test_wanted_ad_title_only():
    assert "wanted_ad" in rules.red_flags(L("Suche ThinkPad T480"))
    assert "wanted_ad" not in rules.red_flags(L("ThinkPad T480", "suche passende Tasche dazu"))


def test_dealer_pattern_and_ev_multiplier():
    l = L("Lenovo ThinkPad T14 G1 | Refurbished | ab 290€", "In verschiedenen Konfigurationen erhältlich.")
    assert "dealer_listing" in rules.screen(l).red_flags
    s = rules.screen(l).model_copy(update={"red_flags": []})
    s2 = rules.screen(l)
    a, b = scoring.score(l, s, Economics()), scoring.score(l, s2, Economics())
    if a.ev > 0:
        assert b.ev == __import__("pytest").approx(a.ev * 0.7, rel=0.01)
    assert "dealer_listing" not in rules.red_flags(L("ThinkPad T14", "Privatverkauf, keine Garantie oder Rücknahme."))


def test_defaults_for_matches_screen():
    assert rules.defaults_for("none") == (0.9, 0.0, 0.75)
    s = rules.screen(L("ThinkPad T480", "Wasserschaden"))
    p, parts, hours = rules.defaults_for(s.fault_category, None)
    assert (p, parts) == (s.p_ok, s.parts_cost_eur)
    assert rules.defaults_for("nonsense")[0] == rules.FAULT_DEFAULTS["unknown"][0]


def test_env_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    monkeypatch.setenv("HUNTBOT_AGENT_PROFILE", "free")
    monkeypatch.setenv("HUNTBOT_TEAM_ENFORCE", "true")
    monkeypatch.setenv("HUNTBOT_TEAM_READER_MODELS", "a/b:free, c/d:free")
    monkeypatch.setenv("HUNTBOT_AGENT_MAX_USD_DAY", "1.25")
    s = load_settings()
    assert s.resolved_provider == "team" and s.agent_profile == "free" and s.team_enforce and s.agent_max_usd_day == 1.25
    assert s.team_chain("reader") == ("a/b:free", "c/d:free")
    assert s.team_chain("skeptic")[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert Settings().team_chain("reader")[0] == "z-ai/glm-5.3-flash"
