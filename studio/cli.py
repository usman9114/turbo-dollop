"""Command line: plan -> render -> review.

    python -m studio plan   --persona personas/example.yaml --count 6
    python -m studio render out/<batch>/plan.json --t2v t2v-1.3b --i2v i2v-480p
    python -m studio review out/<batch>/plan.json
    python -m studio run    --persona personas/example.yaml --count 3   # all three steps
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .persona import load_persona
from .planner import ContentPlan


def _batch_dir(root: Path) -> Path:
    d = root / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_plan(args) -> Path:
    from .planner import plan_batch

    persona, _ = load_persona(args.persona)
    plan = plan_batch(persona, args.count, args.brief or "")
    out = _batch_dir(Path(args.out))
    path = out / "plan.json"
    path.write_text(
        json.dumps({"persona": str(Path(args.persona).resolve()), **plan.model_dump()}, indent=2)
    )
    print(f"Planned {len(plan.shots)} clips -> {path}")
    for s in plan.shots:
        print(f"  [{s.mode}] {s.id}: {s.concept}")
    return path


def _load_plan(path: Path):
    data = json.loads(path.read_text())
    persona, base = load_persona(data["persona"])
    plan = ContentPlan.model_validate({k: v for k, v in data.items() if k != "persona"})
    return plan, persona, base


def cmd_render(args) -> None:
    from .wan import MODELS, WanRenderer

    plan_path = Path(args.plan)
    plan, persona, base = _load_plan(plan_path)
    out = plan_path.parent

    # Render one mode at a time so only one 14B model sits in VRAM.
    for mode, key in (("t2v", args.t2v), ("i2v", args.i2v)):
        shots = [s for s in plan.shots if s.mode == mode]
        if not shots:
            continue
        if MODELS[key][0] != mode:
            sys.exit(f"--{mode} must be a {mode} model, got {key}")
        renderer = WanRenderer(key, offload=args.offload, steps=args.steps)
        for i, shot in enumerate(shots):
            if (out / f"{shot.id}.mp4").exists() and not args.force:
                print(f"skip {shot.id} (exists)")
                continue
            ref = persona.resolved_reference(shot.reference_index, base) if mode == "i2v" else None
            print(f"render [{key}] {shot.id} ...", flush=True)
            renderer.render(shot.wan_prompt, shot.negative_prompt_extra, out / shot.id, ref, seed=args.seed + i)
        renderer.unload()


def cmd_review(args) -> None:
    from .reviewer import review_clip

    plan_path = Path(args.plan)
    plan, _, _ = _load_plan(plan_path)
    out = plan_path.parent
    approved_dir, rejected_dir = out / "approved", out / "rejected"
    approved_dir.mkdir(exist_ok=True)
    rejected_dir.mkdir(exist_ok=True)

    for shot in plan.shots:
        video = out / f"{shot.id}.mp4"
        if not video.exists():
            print(f"missing {shot.id}, not rendered")
            continue
        keyframes = [out / f"{shot.id}_{k}.jpg" for k in ("start", "mid", "end")]
        review = review_clip(keyframes, shot.caption, shot.hashtags)
        dest = approved_dir if review.approved else rejected_dir
        shutil.copy2(video, dest / video.name)
        post = {
            "caption": shot.caption + "\n\n" + " ".join(shot.hashtags),
            "alt_text": shot.alt_text,
            "cover_text": shot.cover_text,
            "review": review.model_dump(),
        }
        (dest / f"{shot.id}.json").write_text(json.dumps(post, indent=2))
        verdict = "APPROVED" if review.approved else "REJECTED"
        print(f"{verdict} {shot.id} (quality {review.quality_score}/10) {'; '.join(review.issues)}")
        if not review.approved and review.suggested_prompt_fix:
            print(f"   fix: {review.suggested_prompt_fix}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="studio", description="Claude + Wan 2.1 Reels pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_plan_args(sp):
        sp.add_argument("--persona", required=True)
        sp.add_argument("--count", type=int, default=5)
        sp.add_argument("--brief", help="extra direction, e.g. 'summer beach week'")
        sp.add_argument("--out", default="out")

    def add_render_args(sp):
        sp.add_argument("--t2v", default="t2v-1.3b", help="t2v-1.3b | t2v-14b")
        sp.add_argument("--i2v", default="i2v-480p", help="i2v-480p | i2v-720p")
        sp.add_argument("--steps", type=int, default=50)
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--offload", action="store_true", help="CPU offload for smaller GPUs")
        sp.add_argument("--force", action="store_true", help="re-render existing clips")

    add_plan_args(sub.add_parser("plan", help="Claude writes prompts and captions"))
    r = sub.add_parser("render", help="Wan 2.1 renders the plan")
    r.add_argument("plan")
    add_render_args(r)
    v = sub.add_parser("review", help="Claude approves/rejects rendered clips")
    v.add_argument("plan")
    run = sub.add_parser("run", help="plan + render + review")
    add_plan_args(run)
    add_render_args(run)

    args = p.parse_args(argv)
    if args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "render":
        cmd_render(args)
    elif args.cmd == "review":
        cmd_review(args)
    else:
        args.plan = str(cmd_plan(args))
        cmd_render(args)
        cmd_review(args)
