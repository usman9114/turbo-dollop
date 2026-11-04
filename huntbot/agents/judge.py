"""Judge: a fixed, ordered decision table that outputs BUY / WATCH / PASS. Pure code, no LLM, no I/O.

Disagreement principles:
 1. Code facts and verified quotes beat model opinions.
 2. Any verified hard veto from any source -> PASS.
 3. Single-source photo negatives, unverified text negatives and rule-vs-agent conflicts -> WATCH.
 4. BUY requires unanimity and every planned agent succeeding.
 5. Failures degrade one level: BUY -> WATCH. If the Reader is missing, the result is UNSCREENED with the rules alert.
"""

from __future__ import annotations

from .. import catalog
from ..config import Economics
from ..models import Listing, Score, Screen
from .ground import Grounding, listing_text, verified
from .schemas import InspectorOutput, ReaderOutput, SkepticOutput, Verdict

THIN_CHARS = 60
SOFT_WATCH = {"vision:lock?", "vision:mismatch", "vision:stock"}


def _skeptic_veto_verified(sk: SkepticOutput, ins: InspectorOutput | None, l: Listing) -> str | None:
    """Claim of the first high-severity objection whose evidence checks out against the listing text / inspector."""
    text = listing_text(l)
    for o in sk.objections:
        if o.severity != "high":
            continue
        ev = o.evidence.strip()
        if ev.lower().startswith("photo:"):
            field, _, value = ev[6:].strip().partition("=")
            if ins is not None and field.strip() in ins.model_dump() and value.strip() \
                    and value.strip().lower() in str(ins.model_dump()[field.strip()]).lower():
                return o.claim
        elif verified(ev, text):
            return o.claim
    return None


def decide(l: Listing, screen: Screen, feats, score: Score, reader: ReaderOutput | None, g: Grounding | None,
           inspector: InspectorOutput | None, skeptic: SkepticOutput | None, soft: list[str], stage: str,
           econ: Economics | None = None, *, inspector_expected: bool = False, skeptic_expected: bool = False,
           models_used: dict[str, str] | None = None, calls_used: int = 0, cost_usd: float = 0.0,
           agents_run: list[str] | None = None) -> Verdict:
    econ = econ or Economics()
    g = g or Grounding()

    def mk(decision: str, reasons: list[str], vetoes: list[str] | None = None, conf: str = "medium",
           degraded: bool = False) -> Verdict:
        return Verdict(decision=decision, confidence=conf, reasons=reasons, vetoes=vetoes or [],
                       agents_run=agents_run or [], models_used=models_used or {}, calls_used=calls_used,
                       degraded=degraded, cost_usd=round(cost_usd, 5))

    # ---- PASS: hard vetoes (code facts or verified evidence) ---------------------------------
    if reader is not None and reader.item_type in ("part", "accessory", "box_or_docs", "wanted_ad") and g.item_type:
        # (usually also rejected via the flag; checked first for the reason text)
        return mk("PASS", [f"not the device: {reader.item_type.replace('_', ' ')}"], [f"reader:{reader.item_type}"], "high")
    if score.rejected:
        flags = [r for r in score.reasons if r.startswith("REJECT:")]
        return mk("PASS", [flags[0] if flags else "rejected by rules"], ["rules:rejected"], "high")
    if reader is not None:
        if not reader.price_is_for_this_item and g.price:
            return mk("PASS", ["price is not for this item (\"ab X €\" / configurations)"], ["reader:price_not_for_item"], "high")
        if reader.seller_type == "dealer" and g.seller and score.mode == "flip" \
                and (score.discount is None or score.discount < 0.35):
            return mk("PASS", ["dealer listing: retail price, not a flip"], ["reader:dealer"], "high")
        if reader.item_type == "multiple_devices" and g.item_type and score.mode == "flip":
            return mk("PASS", ["multiple devices / dealer stock"], ["reader:multiple_devices"], "high")
        if "vision:not_device" in soft and reader.item_type not in ("device", "device_bundle"):
            return mk("PASS", ["photo and text agree it is not a complete device"], ["vision:not_device"], "medium")
    if skeptic is not None and skeptic.verdict == "veto":
        claim = _skeptic_veto_verified(skeptic, inspector, l)
        if claim:
            return mk("PASS", [f"skeptic veto: {claim}"], ["skeptic:veto"], "medium")
    if stage == "final" and l.price is not None and not score.alert:
        disc = f" / discount {score.discount:.0%}" if score.discount is not None else ""
        return mk("PASS", [f"not a deal: EV €{score.ev:.0f}{disc}"], ["code:not_alert"], "high")

    # ---- WATCH: caps (BUY impossible if any holds) -------------------------------------------
    watch: list[tuple[str, bool]] = []     # (reason, driven by code facts / verified evidence)
    if len(l.description.strip()) < THIN_CHARS:
        watch.append(("thin listing: open the ad (userscript re-screens it)", True))
    if l.price is None:
        watch.append(("no price given (VB only)", True))
    entry = catalog.by_key(screen.catalog_key)
    if feats is not None and feats.year is None and not (entry and entry.label == "SOLD"):
        watch.append(("generation unknown", True))
    for s in soft:
        if s.startswith(("conflict:", "unverified:")) or s in SOFT_WATCH:
            watch.append((f"unconfirmed signal: {s}", False))
    if "untested" in soft or (reader is not None and reader.working_status in ("untested", "unknown") and score.mode == "flip"):
        watch.append(("working status not confirmed by the ad", True))
    if reader is not None and reader.item_type not in ("device", "device_bundle"):
        watch.append((f"reader: item looks like '{reader.item_type}' (unverified)", False))
    if reader is not None and reader.working_status == "broken":
        watch.append(("reader: described as broken (unverified)", False))
    if reader is not None and "paypal_only" in reader.scam_signals:
        watch.append(("seller wants PayPal only: ask for cash pickup (Abholung mit Barzahlung möglich?)", False))
    if any(r.startswith("CHECK: >70% under typical") for r in score.reasons):
        watch.append((">70% under typical price: part only, scam or stolen? verify serial + photos", True))
    if (reader is not None and reader.confidence == "low") or (inspector is not None and inspector.confidence == "low"):
        watch.append(("low agent confidence", False))
    if inspector_expected and inspector is None:
        watch.append(("photos not checked", True))
    if skeptic_expected and skeptic is None:
        watch.append(("not double-checked (skeptic unavailable)", True))
    if skeptic is not None and skeptic.verdict in ("downgrade_to_watch", "veto"):
        top = skeptic.objections[0].claim if skeptic.objections else "skeptic doubts the deal"
        watch.append((f"skeptic: {top}", False))
    if reader is not None and reader.seller_type == "dealer":
        watch.append(("seller looks like a dealer", False))

    if watch:
        driven = all(code for _, code in watch)
        degraded = any(w.startswith(("photos not checked", "not double-checked")) for w, _ in watch)
        return mk("WATCH", [w for w, _ in watch][:4], [], "high" if driven else "low", degraded)

    if not score.alert:
        return mk("WATCH", ["not an alert yet"], [], "low")

    # ---- BUY ------------------------------------------------------------------------------
    if reader is None:     # defence in depth: the team never BUYs on rules alone
        return mk("WATCH", ["not screened by the team"], [], "low", True)
    high = (reader is not None and reader.confidence == "high"
            and (inspector is None or inspector.confidence == "high")
            and score.discount is not None and 0.25 <= score.discount <= 0.6)
    disc = f", {score.discount:.0%} under typical" if score.discount is not None else ""
    pre = "" if stage == "final" else " (preliminary)"
    return mk("BUY", [f"deal{pre}: EV €{score.ev:.0f}, max buy €{score.max_buy:.0f}{disc}"], [], "high" if high else "medium")


def should_push(score: Score, verdict: Verdict | None, push_watch: bool = True, enforce: bool = True) -> bool:
    if verdict is None or not enforce:     # shadow mode: alerts behave as before, the verdict is still shown
        return score.alert
    d = verdict.decision
    if d == "BUY":
        return True
    if d == "WATCH":
        return score.alert and push_watch
    if d == "UNSCREENED":
        return score.alert
    return False
