#!/usr/bin/env python3
"""Bundle skills/product-hunter into dist/product-hunter.zip for upload to Claude
(claude.ai → Settings → Capabilities → Skills → Upload).

  python scripts/build_skill.py              # priors only
  python scripts/build_skill.py --db huntbot.db   # also ship YOUR trained price model
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "skills" / "product-hunter"
DIST = ROOT / "dist"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", help="huntbot SQLite DB to train and embed model.json from")
    args = p.parse_args()

    out = DIST / "product-hunter"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(SRC, out, ignore=shutil.ignore_patterns("__pycache__"))
    core = out / "scripts" / "huntbot_core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    for name in ("features.py", "pricemodel.py"):
        shutil.copy(ROOT / "huntbot" / name, core / name)

    if args.db:
        sys.path.insert(0, str(ROOT))
        from huntbot.db import DB
        from huntbot.pricemodel import fit_from_db
        model = fit_from_db(DB(args.db))
        (out / "scripts" / "model.json").write_text(model.to_json())
        print(f"embedded model trained on {model.n} rows")

    zpath = DIST / "product-hunter.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DIST))
    print(f"wrote {zpath}")


if __name__ == "__main__":
    main()
