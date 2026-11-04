import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from huntbot.agents import evals
from huntbot.agents.replay import RecordingTransport, ReplayTransport
from huntbot.config import Settings

from .agents_fakes import FakeOR, reader_json

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_eval_set", ROOT / "evals" / "build_eval_set.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_eval_set_variants(tmp_path):
    b = load_builder()
    pages = [FIX / "search_page_2026.html"]
    rows = b.build(pages, {})
    by = {}
    for r in rows:
        by.setdefault(r["id"], set()).add(r["variant"])
    assert by and all("orig" in v and "-thin" in v for v in by.values())
    assert any("-scam" in v for v in by.values())
    scam = next(r for r in rows if r["variant"] == "-scam")
    assert "PayPal Freunde" in scam["listing"]["description"]
    thin = next(r for r in rows if r["variant"] == "-thin")
    assert thin["listing"]["description"] == ""
    cheap = [r for r in rows if r["variant"] == "-cheap"]
    for c in cheap:                               # cheap = 60% of typical, below the original ask or at least positive
        assert c["listing"]["price"] > 0
    out = tmp_path / "ads.jsonl"
    assert b.main(["--pages", str(FIX), "--out", str(out), "--labels", str(tmp_path / "none.csv")]) == 0
    assert out.read_text().count("\n") == len(rows) or out.read_text().count("\n") >= len(rows)


def ns(tmp_path, **kw):
    base = dict(role="team", profile="free", models=None, set=str(FIX / "agents/synthetic_ads.jsonl"),
                labels=str(FIX / "agents/synthetic_labels.csv"), limit=0, record=None, replay=None, out=str(tmp_path / "res"), yes=True)
    base.update(kw)
    return argparse.Namespace(**base)


def make_env(monkeypatch, tmp_path, fake):
    """Route the eval's HTTP through the scripted FakeOR (recording) instead of the network."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("HUNTBOT_DB", str(tmp_path / "real.db"))
    monkeypatch.chdir(tmp_path)

    def client(settings, args, allow_paid):
        from huntbot.agents.openrouter import OpenRouterClient
        import httpx2 as httpx
        transport = fake.transport
        if args.record:
            transport = RecordingTransport(transport, args.record)
        if args.replay:
            transport = ReplayTransport(args.replay)
        return OpenRouterClient("k", http=httpx.Client(transport=transport), allow_paid=True), transport
    monkeypatch.setattr(evals, "_client", client)


def test_record_then_replay_team_eval_has_no_false_buy(tmp_path, monkeypatch, capsys):
    fake = FakeOR(reader=lambda body: reader_json(
        item_type="part", item_type_evidence=["Tastatur für ThinkPad"]) if "Tastatur" in body["messages"][1]["content"]
        else reader_json(seller_type="dealer", seller_evidence=["Unsere Vorteile"], price_is_for_this_item=False,
                         price_evidence=["ab 290€"]) if "Unsere Vorteile" in body["messages"][1]["content"] else reader_json())
    make_env(monkeypatch, tmp_path, fake)
    rec = tmp_path / "rec"
    assert evals.main(ns(tmp_path, record=str(rec))) == 0
    printed = capsys.readouterr().out
    assert "false_buy" in printed and "wrote" in printed
    assert list(rec.glob("*.json"))
    n_live = fake.count
    out_file = next((tmp_path / "res").glob("*-team.json"))
    data = json.loads(out_file.read_text())
    res = data["results"][0]
    assert res["false_buy"] == 0 and res["ads"] == 4 and res["pass_recall"] == 1.0
    # replay: no network, same numbers
    assert evals.main(ns(tmp_path, replay=str(rec))) == 0
    again = json.loads(next((tmp_path / "res").glob("*-team.json")).read_text())["results"][0]
    assert again["false_buy"] == 0 and again["confusion"] == res["confusion"]
    assert fake.count == n_live                              # replay answered everything itself


def test_reader_eval_ranks_models_and_measures_grounding(tmp_path, monkeypatch, capsys):
    good = lambda body: reader_json()
    fake = FakeOR(reader=good)
    make_env(monkeypatch, tmp_path, fake)
    args = ns(tmp_path, role="reader", models="google/gemma-4-31b-it:free,z-ai/glm-5.3-flash")
    assert evals.main(args) == 0
    text = capsys.readouterr().out
    assert "rank_score" in text and "gemma-4-31b-it:free" in text and "glm-5.3-flash" in text
    served = {r["model"] for r in fake.requests}
    assert served == {"google/gemma-4-31b-it:free", "z-ai/glm-5.3-flash"}          # each model alone
    assert all("models" not in r for r in fake.requests)                              # no fallbacks
    res = json.loads(next((tmp_path / "res").glob("*-reader.json")).read_text())["results"]
    assert res[0]["grounding"] is not None and res[0]["invalid_rate"] == 0.0


def test_missing_set_is_a_clear_error(tmp_path, capsys):
    assert evals.main(ns(tmp_path, set=str(tmp_path / "nope.jsonl"))) == 2
    assert "build_eval_set" in capsys.readouterr().out


def test_label_for_derivation():
    labels = evals.load_labels(ROOT / "evals" / "labels.csv")
    assert labels[("3353989836", "orig")]["seller_type"] == "dealer"
    row = {"id": "123", "variant": "-scam"}
    assert evals.label_for(row, labels)["expected"] == "PASS"
    assert evals.label_for({"id": "123", "variant": "-cheap"}, labels)["expected"] == "NOT_PASS"
    assert evals.label_for({"id": "3560335006", "variant": "-cheap"}, labels)["expected"] == "ANY"
