"""Merge sources -> data/signs.json + data/lesson.json, sanitize SVGs into signs/, render app/index.html."""
import json, re, sys, os, base64, shutil
sys.path.insert(0, os.path.dirname(__file__))
from fixes import apply
WORK, DL = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
res = {r["num"]: r for r in json.load(open(f"{WORK}/resolved.json"))}
qs = apply(json.load(open(f"{WORK}/questions.json")))
lesson = json.load(open(f"{WORK}/lesson.json"))

def clean_svg(b):
    s = b.decode("utf-8", "replace")
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", s)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<metadata.*?</metadata>", "", s, flags=re.S)
    return re.sub(r">\s+<", "><", s).strip()

TITLES = {1: "the 50 signs that matter most in city traffic", 2: "lane markings, warnings, restrictions and motorway basics",
          3: "supplementary plates, special lanes and direction signs"}
signs = []
for q in qs:
    r = res[q["num"]]; p = os.path.join(DL, q["num"] + ".svg")
    if not os.path.exists(p): print("skip (no image)", q["num"]); continue
    svg = clean_svg(open(p, "rb").read())
    open(os.path.join(ROOT, "signs", q["num"] + ".svg"), "w").write(svg)
    signs.append({"num": q["num"], "week": r["week"], "name_de": q["name_de"], "name_en": q["name_en"],
        "category": q["category"].split(" (")[0], "question": q["question"], "options": q["options"], "answer": q["answer"],
        "explanation": q["explanation"], "tip": q["tip"], "confused_with": q.get("confused_with", ""),
        "source": "https://commons.wikimedia.org/wiki/File:" + r["file"].replace(" ", "_"),
        "img": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()})
# Week 3 had spare entries; keep 50 per week max
by_week = {}
for s in signs: by_week.setdefault(s["week"], []).append(s)
signs = [s for w in sorted(by_week) for s in by_week[w][:50]]
for s in signs:
    if s["confused_with"] not in {x["num"] for x in signs}: s["confused_with"] = ""
print({w: len(v[:50]) for w, v in by_week.items()})
json.dump([{k: v for k, v in s.items() if k != "img"} for s in signs], open(os.path.join(ROOT, "data/signs.json"), "w"), ensure_ascii=False, indent=1)
json.dump(lesson, open(os.path.join(ROOT, "data/lesson.json"), "w"), ensure_ascii=False, indent=1)
DATA = {"start": "2026-11-05", "lessonDate": "2026-11-06", "weekTitles": TITLES,
        "signs": signs, "lesson": {"guide": lesson["guide"], "scenarios": [{k: v for k, v in q.items() if k != "review"} for q in lesson["scenarios"]]}}
tpl = open(os.path.join(ROOT, "app/template.html")).read()
out = tpl.replace("/*__DATA__*/null", json.dumps(DATA, ensure_ascii=False).replace("</", "<\\/"))
open(os.path.join(ROOT, "app/index.html"), "w").write(out)
print("wrote app/index.html", round(len(out) / 1024), "KB")
