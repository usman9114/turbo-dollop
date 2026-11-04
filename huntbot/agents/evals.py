"""`huntbot agents-eval`: measure Reader / Inspector / Skeptic / the whole team on a labelled eval set.

The eval set (scraped ad text) and results stay OUT of git (evals/data/, evals/results/). Labels are CSV:
id,variant,item_type,working,seller_type,scam,price_for_item,expected,notes   (expected: PASS|NOT_PASS|NOT_BUY|ANY)
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

import httpx2 as httpx

from ..config import Settings, load_settings
from ..db import DB
from ..models import Listing
from . import ground, prompts
from .budget import Budget
from .openrouter import OpenRouterClient
from .registry import Registry
from .replay import RecordingTransport, ReplayTransport
from .roles import RoleRunner, specs_from_settings
from .store import AgentStore, content_hash
from .team import ScreeningTeam

ITEM_GROUP = {"device": "device", "device_bundle": "device", "part": "part_or_accessory", "accessory": "part_or_accessory",
              "box_or_docs": "part_or_accessory", "multiple_devices": "dealer_multi", "wanted_ad": "wanted", "unclear": "unclear"}
WORK_GROUP = {"working": "working", "working_minor_issues": "working", "broken": "broken", "untested": "unknown", "unknown": "unknown"}
PHOTO_OK = {"part": {"part_only", "accessory_only"}, "accessory": {"part_only", "accessory_only"},
            "device": {"real_device", "stock_or_render"}}
EST_CALLS = {"reader": 1.1, "inspector": 1.1, "skeptic": 1.1, "team": 2.0}
EST_USD = {"reader": 0.0002, "inspector": 0.0004, "skeptic": 0.004, "team": 0.002}


def load_set(path: Path, limit: int = 0) -> list[dict]:
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        r["obj"] = Listing(**r["listing"])
    return rows[:limit] if limit else rows


def load_labels(path: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    if Path(path).exists():
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                out[(row["id"], row.get("variant") or "orig")] = row
    return out


def label_for(row: dict, labels: dict) -> dict:
    """Exact label, else derived from the base ad (default: private working device) and the variant."""
    ad, var = str(row["id"]), row.get("variant", "orig")
    if (ad, var) in labels:
        return labels[(ad, var)]
    base = dict(labels.get((ad, "orig"), {"id": ad, "item_type": "device", "working": "working", "seller_type": "private",
                                          "scam": "no", "price_for_item": "yes", "expected": "PASS"}))
    if var == "orig":
        return base
    plain = base.get("item_type") == "device" and base.get("working") in ("working", "") and base.get("seller_type") != "dealer"
    if var.endswith("cheap"):
        base["expected"] = "NOT_PASS" if plain else "ANY"
    elif var.endswith("scam"):
        base.update(scam="yes", expected="PASS")
    elif var.endswith("broken"):
        base.update(working="broken", expected="ANY")
    elif var.endswith("thin"):
        base["expected"] = "NOT_BUY"
    return base


def _yn(s: str) -> str:
    s = (s or "").strip().lower()
    return "yes" if s.startswith("yes") else ("no" if s.startswith("no") else "")


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def acc(pairs: list[tuple[str, str]]) -> float | None:
    pairs = [(a, b) for a, b in pairs if b not in ("", "-", None)]
    return sum(a == b for a, b in pairs) / len(pairs) if pairs else None


def _client(settings: Settings, args, allow_paid: bool):
    if args.replay:
        transport = ReplayTransport(args.replay)
    else:
        if not settings.openrouter_key:
            raise SystemExit("OPENROUTER_API_KEY is not set (use --replay DIR for an offline run)")
        transport = httpx.HTTPTransport()
        if args.record:
            transport = RecordingTransport(transport, args.record)
    http = httpx.Client(transport=transport, timeout=httpx.Timeout(settings.or_timeout, connect=10.0))
    return OpenRouterClient(settings.openrouter_key or "replay", http=http, timeout=settings.or_timeout,
                            allow_paid=allow_paid or settings.or_allow_paid or settings.agent_profile == "glm"), transport


def _fresh_team(settings: Settings, args, chains: dict | None = None, allow_paid: bool = True) -> ScreeningTeam:
    """A team on a throw-away in-memory DB: no cross-model cache contamination."""
    client, _ = _client(settings, args, allow_paid)
    db = DB(":memory:")
    sleep = (lambda s: None) if args.replay else None
    kw = {"sleep": sleep} if sleep else {}
    store = AgentStore(db)
    budget = Budget(store, daily_cap=10**6, run_cap=10**6, run_seconds=10**6, reserve_interactive=0, max_calls_run=10**6,
                    max_usd_day=max(settings.agent_max_usd_day, 0.01), always_lenient=True, **kw)
    reg = Registry(store)
    team = ScreeningTeam(settings, db, client, budget=budget, registry=reg, specs=specs_from_settings(settings, chains), **kw)
    return team


def _latencies(db: DB) -> list[float]:
    return [r["latency_ms"] for r in db.conn.execute("SELECT latency_ms FROM llm_calls WHERE ok=1")]


def eval_role(settings: Settings, args, role: str, model: str | None, rows: list[dict], labels: dict) -> dict:
    chain = [model] if model else list(settings.team_chain(role))
    chains = {r: list(settings.team_chain(r)) for r in ("reader", "inspector", "skeptic")}
    chains[role] = chain
    team = _fresh_team(settings, args, chains, allow_paid=True)
    team.budget.start_run()
    spec = team.specs[role]
    n = invalid = 0
    item, work, seller, scam, pfi, photo = [], [], [], [], [], []
    quotes_ok = quotes_all = 0
    for row in rows:
        l: Listing = row["obj"]
        lab = label_for(row, labels)
        if role == "inspector":
            if not [u for u in l.image_urls if u.startswith("https://")]:
                continue
            msgs, h = prompts.inspector_messages(l), content_hash("inspector", l)
        else:
            msgs, h = prompts.reader_messages(l), content_hash("reader", l)
        n += 1
        try:
            out, _ = team.runner.run(spec, msgs, h, "eval", l.key)
        except Exception:
            out = None
        if out is None:
            invalid += 1
            continue
        if role == "reader":
            item.append((ITEM_GROUP.get(out.item_type, "unclear"), ITEM_GROUP.get(lab.get("item_type", ""), lab.get("item_type", ""))))
            work.append((WORK_GROUP[out.working_status], WORK_GROUP.get(lab.get("working", ""), lab.get("working", ""))))
            seller.append((out.seller_type, lab.get("seller_type", "")))
            scam.append(("yes" if out.scam_signals else "no", _yn(lab.get("scam", ""))))
            pfi.append(("yes" if out.price_is_for_this_item else "no", _yn(lab.get("price_for_item", ""))))
            g = ground.verify_reader(out, l)
            quotes_ok, quotes_all = quotes_ok + g.ok, quotes_all + g.total
        else:
            want = PHOTO_OK.get(lab.get("item_type", ""))
            if want:
                photo.append(("ok" if out.photo_kind in want else "bad", "ok"))
    calls = team.db.conn.execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    lat = _latencies(team.db)
    res = {"role": role, "model": model or "+".join(chain), "ads": n, "invalid_rate": round(invalid / n, 3) if n else 0.0,
           "p50_ms": int(pct(lat, 0.5)), "p95_ms": int(pct(lat, 0.95)), "calls_per_ad": round(calls / n, 2) if n else 0.0,
           "cost_usd": round(team.budget.usd_today(), 5)}
    if role == "reader":
        res.update(item=acc(item), working=acc(work), seller=acc(seller), scam=acc(scam), price_for_item=acc(pfi),
                   grounding=round(quotes_ok / quotes_all, 3) if quotes_all else 1.0)
        res["rank_score"] = round(0.3 * (res["item"] or 0) + 0.3 * (res["working"] or 0) + 0.15 * (res["scam"] or 0)
                                  + 0.15 * (res["seller"] or 0) + 0.1 * res["grounding"] - 0.3 * res["invalid_rate"], 4)
    else:
        res["photo_agreement"] = acc(photo)
        res["rank_score"] = round((res["photo_agreement"] or 0) - 0.3 * res["invalid_rate"], 4)
    res["_calls"] = _dump_calls(team.db)
    return res


def eval_team(settings: Settings, args, rows: list[dict], labels: dict, model: str | None = None) -> dict:
    chains = {r: list(settings.team_chain(r)) for r in ("reader", "inspector", "skeptic")}
    if model:
        chains["reader"], chains["skeptic"] = [model], [model]
    team = _fresh_team(settings, args, chains, allow_paid=True)
    team.budget.start_run()
    price_model = None
    confusion: Counter = Counter()
    false_buy = pass_hit = pass_n = cheap_hit = cheap_n = 0
    skeptic_run = 0
    details = []
    for row in rows:
        l: Listing = row["obj"]
        lab = label_for(row, labels)
        exp = (lab.get("expected") or "ANY").upper()
        team.db.upsert_listing(l, "")
        out = team.screen(l, price_model, purpose="eval")
        if out.verdict is not None:
            dec = out.verdict.decision
            skeptic_run += "skeptic" in out.verdict.agents_run
        else:
            dec = "RULES_ALERT" if out.score.alert else "RULES_PASS"
        confusion[(exp, dec)] += 1
        bad_buy = dec == "BUY" and (exp in ("PASS", "NOT_BUY") or lab.get("working") == "broken")
        false_buy += bad_buy
        if exp == "PASS":
            pass_n += 1
            pass_hit += dec in ("PASS", "RULES_PASS")
        if exp == "NOT_PASS":
            cheap_n += 1
            cheap_hit += dec not in ("PASS", "RULES_PASS")
        details.append({"id": row["id"], "variant": row.get("variant", "orig"), "expected": exp, "decision": dec,
                        "reasons": out.verdict.reasons[:2] if out.verdict else out.score.reasons[:2]})
    calls = team.db.conn.execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    lat = _latencies(team.db)
    n = len(rows)
    return {"role": "team", "model": model or "configured", "ads": n, "false_buy": false_buy,
            "pass_recall": round(pass_hit / pass_n, 3) if pass_n else None,
            "non_pass_recall_cheap": round(cheap_hit / cheap_n, 3) if cheap_n else None,
            "calls_per_ad": round(calls / n, 2) if n else 0.0, "skeptic_runs": skeptic_run,
            "p50_ms": int(pct(lat, 0.5)), "p95_ms": int(pct(lat, 0.95)), "cost_usd": round(team.budget.usd_today(), 5),
            "confusion": {f"{e}->{d}": c for (e, d), c in sorted(confusion.items())}, "details": details,
            "_calls": _dump_calls(team.db)}


def _dump_calls(db: DB) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM llm_calls")]


def _merge_calls(settings: Settings, calls: list[dict]) -> None:
    """Eval calls count against the real daily budget."""
    if not calls:
        return
    try:
        real = AgentStore(DB(settings.db_path))
    except Exception:
        return
    for c in calls:
        c = {k: v for k, v in c.items() if k != "id"}
        cols = ",".join(c)
        real.db.conn.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({','.join('?' * len(c))})", list(c.values()))
    real.db.conn.commit()


def format_table(results: list[dict]) -> str:
    if not results:
        return "(no results)"
    role = results[0]["role"]
    if role == "reader":
        cols = ["model", "rank_score", "item", "working", "seller", "scam", "price_for_item", "grounding", "invalid_rate", "p50_ms", "calls_per_ad"]
    elif role == "inspector":
        cols = ["model", "rank_score", "photo_agreement", "invalid_rate", "p50_ms", "p95_ms", "calls_per_ad"]
    else:
        cols = ["model", "ads", "false_buy", "pass_recall", "non_pass_recall_cheap", "calls_per_ad", "skeptic_runs", "p50_ms", "cost_usd"]
    rows = [[str(r.get(c, "")) if r.get(c) is not None else "-" for c in cols] for r in results]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))
    return "\n".join([line(cols), line(["-" * w for w in widths])] + [line(r) for r in rows])


def main(args) -> int:
    settings = load_settings()
    if args.profile:
        settings = dataclasses.replace(settings, agent_profile=args.profile, team_reader_models=None,
                                       team_inspector_models=None, team_skeptic_models=None)
    set_path = Path(args.set)
    if not set_path.exists():
        print(f"eval set not found: {set_path}\nBuild it with: python evals/build_eval_set.py --pages <dir of saved result pages> --out {set_path}")
        return 2
    rows = load_set(set_path, args.limit)
    labels = load_labels(Path(args.labels))
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else [None]
    est_calls = int(len(rows) * EST_CALLS[args.role] * len(models) + 0.999)
    est_usd = est_calls * EST_USD[args.role]
    print(f"eval: role={args.role} ads={len(rows)} models={[m or 'configured chain' for m in models]} "
          f"profile={settings.agent_profile}\nestimate: ~{est_calls} requests, up to ~${est_usd:.3f} if all paid")
    if not args.replay and not args.yes:
        try:
            store = AgentStore(DB(settings.db_path))
            b = Budget(store, daily_cap=settings.agent_daily_cap, reserve_interactive=0)
            if any(m is None or m.endswith(":free") for m in models) and est_calls > b.remaining_today() \
                    and all((m or "").endswith(":free") or m is None and settings.agent_profile == "free" for m in models):
                print(f"refusing: only {b.remaining_today()} free requests left today (use --yes to try anyway)")
                return 2
            if est_usd > settings.agent_max_usd_day - b.usd_today():
                print(f"refusing: estimate exceeds the remaining daily paid cap ${settings.agent_max_usd_day - b.usd_today():.2f} "
                      f"(HUNTBOT_AGENT_MAX_USD_DAY); use --yes to run until the cap stops it")
                return 2
        except Exception:
            pass
    results = []
    for m in models:
        if args.role in ("reader", "inspector"):
            results.append(eval_role(settings, args, args.role, m, rows, labels))
        elif args.role == "skeptic":
            results.append(eval_team(settings, args, rows, labels, m))
        else:
            results.append(eval_team(settings, args, rows, labels, m))
    if args.role in ("reader", "inspector"):
        results.sort(key=lambda r: (-r["rank_score"], r["p50_ms"]))
    print()
    print(format_table(results))
    if args.role in ("team", "skeptic"):
        for r in results:
            print(f"\nconfusion ({r['model']}): {r['confusion']}")
            if r["false_buy"]:
                print(f"!! FALSE BUY: {r['false_buy']} (must be 0 before enabling HUNTBOT_TEAM_ENFORCE)")
    if not args.replay:
        for r in results:
            _merge_calls(settings, r.get("_calls", []))
    for r in results:
        r.pop("_calls", None)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}-{args.role}.json"
    out_path.write_text(json.dumps({"role": args.role, "profile": settings.agent_profile, "results": results},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0
