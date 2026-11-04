"""Table-driven tests of the code Judge (docs/agent-team-plan.md §2.5): no HTTP, constructed agent outputs."""

import pytest

from huntbot import features, rules
from huntbot.agents import judge
from huntbot.agents.ground import Grounding
from huntbot.agents.schemas import InspectorOutput, ReaderOutput, SkepticOutput, Verdict
from huntbot.config import Settings
from huntbot.models import Score

from .agents_fakes import DESC, TITLE, inspector_json, listing, reader_json, skeptic_json

L = listing()
SCREEN = rules.screen(L)
FEATS = features.extract(L.title, L.description)
OK = Grounding(item_type=True, working=True, seller=True, price=True, lock=True, scam=True, purchase=True)


def sc(**kw) -> Score:
    base = dict(ev=79, ev_per_hour=100, roi=1, buy_estimate=150, max_buy=200, resale=290, net_sale=250, cost_in=170, hours=0.75,
                p_ok=0.9, alert=True, rejected=False, reasons=["x"], mode="flip", fair_ask=320, discount=0.45)
    base.update(kw)
    return Score(**base)


def R(**kw): return ReaderOutput.model_validate(reader_json(**kw))
def I(**kw): return InspectorOutput.model_validate(inspector_json(**kw))
def S(**kw): return SkepticOutput.model_validate(skeptic_json(**kw))


def decide(score=None, reader=None, g=OK, ins=None, sk=None, soft=(), stage="final", listing_=L, feats=FEATS, **kw):
    return judge.decide(listing_, SCREEN, feats, score or sc(), reader if reader is not None else R(), g, ins, sk, list(soft), stage, **kw)


VETO = [{"kind": "broken", "claim": "dead", "evidence": "Funktioniert einwandfrei", "severity": "high"}]

CASES = [
    # --- PASS ---
    ("rejected", dict(score=sc(rejected=True, reasons=["REJECT: activation_lock"])), "PASS"),
    ("reader part verified", dict(reader=R(item_type="part")), "PASS"),
    ("reader accessory verified", dict(reader=R(item_type="accessory")), "PASS"),
    ("reader wanted verified", dict(reader=R(item_type="wanted_ad")), "PASS"),
    ("price not for item", dict(reader=R(price_is_for_this_item=False)), "PASS"),
    ("dealer flip low discount", dict(reader=R(seller_type="dealer"), score=sc(discount=0.2)), "PASS"),
    ("multiple devices flip", dict(reader=R(item_type="multiple_devices")), "PASS"),
    ("vision not device + reader not device", dict(reader=R(item_type="unclear"), soft=["vision:not_device"]), "PASS"),
    ("skeptic verified veto", dict(sk=S(verdict="veto", objections=VETO)), "PASS"),
    ("final not alert", dict(score=sc(alert=False, ev=5, discount=0.1)), "PASS"),
    # --- WATCH ---
    ("unverified part claim", dict(reader=R(item_type="part"), g=Grounding()), "WATCH"),
    ("vision not device but reader says device", dict(soft=["vision:not_device"]), "BUY"),   # alone it is ignored: Reader must agree
    ("thin listing", dict(listing_=listing(desc="Laptop ok")), "WATCH"),
    ("no price", dict(listing_=listing(price=None)), "WATCH"),
    ("generation unknown", dict(feats=features.extract("Laptop", "")), "WATCH"),
    ("conflict soft", dict(soft=["conflict:working"]), "WATCH"),
    ("vision lock?", dict(soft=["vision:lock?"]), "WATCH"),
    ("untested", dict(reader=R(working_status="untested")), "WATCH"),
    ("status unknown in flip", dict(reader=R(working_status="unknown")), "WATCH"),
    ("paypal only", dict(reader=R(scam_signals=["paypal_only"])), "WATCH"),
    ("too cheap check", dict(score=sc(reasons=["CHECK: >70% under typical - part only"])), "WATCH"),
    ("reader low conf", dict(reader=R(confidence="low")), "WATCH"),
    ("inspector low conf", dict(ins=I(confidence="low")), "WATCH"),
    ("photos required but failed", dict(inspector_expected=True), "WATCH"),
    ("skeptic required but failed", dict(skeptic_expected=True), "WATCH"),
    ("skeptic downgrade", dict(sk=S(verdict="downgrade_to_watch")), "WATCH"),
    ("skeptic unverified veto", dict(sk=S(verdict="veto", objections=[dict(VETO[0], evidence="not in the ad")])), "WATCH"),
    ("dealer high discount", dict(reader=R(seller_type="dealer"), score=sc(discount=0.5)), "WATCH"),
    # --- BUY ---
    ("clean buy", dict(ins=I(), sk=S()), "BUY"),
    ("clean buy no photos needed", dict(), "BUY"),
]


@pytest.mark.parametrize("name,kw,expected", CASES, ids=[c[0] for c in CASES])
def test_decision_table(name, kw, expected):
    assert decide(**kw).decision == expected


def test_confidence_and_headline():
    v = decide(ins=I(), sk=S())
    assert v.confidence == "high" and v.reasons[0].startswith("deal:") and "EV €79" in v.reasons[0]
    assert decide(ins=I(confidence="medium"), sk=S()).confidence == "medium"
    assert decide(score=sc(discount=0.8)).confidence == "medium"
    assert decide(listing_=listing(desc="x")).confidence == "high"                      # code fact
    assert decide(soft=["conflict:working"]).confidence == "low"                         # soft signal


def test_preliminary_buy_marker_and_non_alert_cannot_buy():
    assert "preliminary" in decide(stage="inspector").reasons[0]
    assert decide(score=sc(alert=False), stage="inspector").decision == "WATCH"


def test_pass_beats_watch_order():
    v = decide(reader=R(item_type="part", confidence="low"), listing_=listing(desc="x"))
    assert v.decision == "PASS"


@pytest.mark.parametrize("decision,alert,watch,enforce,expected", [
    ("BUY", False, True, True, True), ("WATCH", True, True, True, True), ("WATCH", True, False, True, False),
    ("WATCH", False, True, True, False), ("UNSCREENED", True, True, True, True), ("UNSCREENED", False, True, True, False),
    ("PASS", True, True, True, False), ("PENDING", True, True, True, False),
    ("PASS", True, True, False, True), ("PENDING", True, True, False, True), ("BUY", False, True, False, False),
])
def test_should_push(decision, alert, watch, enforce, expected):
    assert judge.should_push(sc(alert=alert), Verdict(decision=decision), watch, enforce) is expected
    assert judge.should_push(sc(alert=alert), None, watch, enforce) is alert


def test_never_buy_without_reader():
    from huntbot.agents import judge
    from huntbot.config import Economics
    from huntbot.models import Listing, Score, Screen
    l = Listing(source="kleinanzeigen", ad_id="1", url="u", title="ThinkPad T14 Gen 2", price=150.0,
                description="Funktioniert einwandfrei, mit Netzteil, Windows 11, 16 GB RAM, 512 GB SSD, kaum benutzt")
    sc = Score(ev=80, ev_per_hour=100, roi=0.5, buy_estimate=150, max_buy=200, resale=300, net_sale=250, cost_in=160,
               hours=0.75, p_ok=0.9, alert=True, rejected=False, mode="flip", discount=0.45)
    scr = Screen(device_model="ThinkPad T14 Gen 2", catalog_key="thinkpad-t14-g2", fault_summary="", fault_category="none",
                 p_ok=0.9, parts_cost_eur=0, repair_hours=0.75, red_flags=[], confidence="low", questions_for_seller=[])
    v = judge.decide(l, scr, None, sc, None, None, None, None, [], "final", Economics())
    assert v.decision != "BUY"
