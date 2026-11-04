"""Build the eval set from saved Kleinanzeigen result pages (data stays OUT of git: evals/data/ is ignored).

  python evals/build_eval_set.py --pages DIR_WITH_HTML [--db real.db] [--labels evals/labels.csv] --out evals/data/ads.jsonl

Each ad becomes an "orig" row plus counterfactual variants (generated with a rules-only pipeline to get the typical ask):
  -cheap  price = 60% of the typical ask (device/working/private ads only)  -> must not be PASS
  -scam   + "Bezahlung nur PayPal Freunde, nur Versand, bin beruflich im Ausland."  -> must be PASS
  -broken + "Geht nach ein paar Minuten einfach aus."                          -> Reader must say broken
  -thin   description removed                                                   -> must never be BUY
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huntbot.config import Settings  # noqa: E402
from huntbot.db import DB  # noqa: E402
from huntbot.models import Listing  # noqa: E402
from huntbot.pipeline import Pipeline  # noqa: E402
from huntbot.sources.kleinanzeigen_live import parse_search_html  # noqa: E402

SCAM_TAIL = " Bezahlung nur PayPal Freunde, nur Versand, bin beruflich im Ausland."
BROKEN_TAIL = " Geht nach ein paar Minuten einfach aus."


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh) if (r.get("variant") or "orig") == "orig"}


def build(pages: list[Path], labels: dict[str, dict], db_path: Path | None = None) -> list[dict]:
    listings: dict[str, Listing] = {}
    for page in pages:
        for l in parse_search_html(page.read_text(encoding="utf-8", errors="replace")):
            listings.setdefault(l.ad_id, l)
    if db_path and db_path.exists():          # prefer longer descriptions already stored
        import sqlite3
        con = sqlite3.connect(db_path)
        for ad_id, desc in con.execute("SELECT ad_id, description FROM listings WHERE source='kleinanzeigen'"):
            if ad_id in listings and len(desc or "") > len(listings[ad_id].description):
                listings[ad_id] = dataclasses.replace(listings[ad_id], description=desc)
    pipe = Pipeline(Settings(), DB(":memory:"))
    rows: list[dict] = []
    for ad_id, l in listings.items():
        rows.append({"id": ad_id, "variant": "orig", "listing": dataclasses.asdict(l)})
        r = pipe.process(l, use_llm=False, notify=False)
        lab = labels.get(ad_id, {"item_type": "device", "working": "working", "seller_type": "private"})
        plain = (lab.get("item_type") == "device" and lab.get("working") in ("working", "") and lab.get("seller_type") != "dealer"
                 and not r.score.rejected)
        if plain and r.score.fair_ask and l.price:
            cheap = dataclasses.replace(l, price=float(round(0.6 * r.score.fair_ask)), ad_id=f"{ad_id}-cheap")
            rows.append({"id": ad_id, "variant": "-cheap", "listing": dataclasses.asdict(cheap)})
        if lab.get("item_type") == "device":
            rows.append({"id": ad_id, "variant": "-scam", "listing": dataclasses.asdict(
                dataclasses.replace(l, description=l.description + SCAM_TAIL, ad_id=f"{ad_id}-scam"))})
            if lab.get("working") != "broken":
                rows.append({"id": ad_id, "variant": "-broken", "listing": dataclasses.asdict(
                    dataclasses.replace(l, description=l.description + BROKEN_TAIL, ad_id=f"{ad_id}-broken"))})
        rows.append({"id": ad_id, "variant": "-thin", "listing": dataclasses.asdict(
            dataclasses.replace(l, description="", ad_id=f"{ad_id}-thin"))})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", required=True, help="directory with saved result pages (*.html)")
    ap.add_argument("--db", help="optional huntbot DB with fuller descriptions")
    ap.add_argument("--labels", default=str(Path(__file__).with_name("labels.csv")))
    ap.add_argument("--out", default="evals/data/ads.jsonl")
    args = ap.parse_args(argv)
    pages = sorted(Path(args.pages).glob("*.html"))
    if not pages:
        print(f"no *.html pages in {args.pages}")
        return 2
    rows = build(pages, load_labels(Path(args.labels)), Path(args.db) if args.db else None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows ({len({r['id'] for r in rows})} ads) to {out}  (do not commit: contains third-party ad text)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
