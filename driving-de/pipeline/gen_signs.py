"""Step 2 (Sonnet): write one MCQ + explanation per sign. Step 3 (Haiku): blind-answer + critique."""
import json, sys, concurrent.futures as cf
from openrouter import SONNET, HAIKU, chat_json

signs = json.load(open(sys.argv[1])); out = sys.argv[2]

WRITER = """You are a German driving-school theory instructor writing a multiple-choice quiz for an experienced
driver who converted a foreign licence and is now taking city lessons in Germany. Be legally precise to the current
StVO (Straßenverkehrs-Ordnung, incl. 2020 amendments). English text, but always give the official German sign name.
Never invent fines or numbers you are unsure of. Return ONLY JSON."""

WRITER_TASK = """For each sign below write an object:
{"num": "<as given>",
 "name_de": "<official German name, short>",
 "name_en": "<clear English name>",
 "category": "Gefahrzeichen (warning) | Vorschriftzeichen (regulatory) | Richtzeichen (informational) | Zusatzzeichen (supplementary plate) | Markierung (road marking)",
 "question": "<question shown under the sign image; vary: 'What does this sign mean?', 'What must you do when you see this sign?', 'Which statement is correct?'>",
 "options": ["4 options; exactly ONE correct; distractors must be plausible (meanings of similar-looking signs) but clearly wrong"],
 "answer": <index 0-3 of correct option; vary the position>,
 "explanation": "<2-4 sentences: what it means, what exactly you must do, until when it applies>",
 "tip": "<one practical city-driving tip or common trap, e.g. what examiners/instructors watch for>",
 "confused_with": "<StVO number of the sign most often confused with it, or empty>"}
Signs (StVO number | official Commons name):
"""

REVIEWER = """You are a strict German traffic-law (StVO) fact-checker. You check quiz questions about German road
signs. Return ONLY JSON."""

REVIEW_TASK = """For each question: (1) answer it yourself WITHOUT looking at the stated answer (pick an index),
(2) check the explanation and tip for factual errors against current StVO, (3) check that exactly one option is correct.
Return a list of {"num": ..., "my_answer": <index>, "ok": true|false, "issues": "<empty if ok, else concise problem>"}.
Questions (sign identified by StVO number and official name since you cannot see the image):
"""

def batches(L, n):
    for i in range(0, len(L), n): yield L[i:i + n]

def write(batch):
    lines = "\n".join(f'{s["num"]} | {s["de"] or s["file"]}' for s in batch)
    return chat_json(SONNET, WRITER, WRITER_TASK + lines, max_tokens=12000)

def review(batch):
    qs = [{k: q[k] for k in ("num", "name_de", "question", "options", "explanation", "tip")} for q in batch]
    return chat_json(HAIKU, REVIEWER, REVIEW_TASK + json.dumps(qs, ensure_ascii=False), max_tokens=8000, temperature=0)

with cf.ThreadPoolExecutor(6) as ex:
    qs = [q for r in ex.map(write, batches(signs, 10)) for q in r]
print("Sonnet wrote", len(qs))
with cf.ThreadPoolExecutor(6) as ex:
    rv = {r["num"]: r for b in ex.map(review, batches(qs, 10)) for r in b}
flags = 0
for q in qs:
    r = rv.get(q["num"], {})
    q["review"] = r
    if not r.get("ok", False) or r.get("my_answer") != q["answer"]:
        flags += 1
print("Haiku flagged", flags)
json.dump(qs, open(out, "w"), ensure_ascii=False, indent=1)
