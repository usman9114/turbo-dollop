"""Sonnet: lesson guide + scenario MCQs strictly from city_facts.md. Haiku: blind review."""
import json, sys
from openrouter import SONNET, HAIKU, chat_json
facts = open("city_facts.md").read(); out = sys.argv[1]
SYS = "You are a calm, precise German driving instructor (Fahrlehrer). Use ONLY the facts provided; do not add rules or numbers that are not in them. English text, German key terms in brackets. Return ONLY JSON."
guide = chat_json(SONNET, SYS, facts + """

Write a study guide for an experienced driver (highway/long-distance experience, licence converted, no German lessons) whose first 90-min CITY lesson is tomorrow. JSON:
{"sections": [{"title": "...", "icon": "<one emoji>", "points": [{"head": "<short bold rule>", "body": "<1-2 sentences, concrete>"}]}],
 "checklist": ["<things to do tomorrow, short>"],
 "mnemonics": [{"name": "...", "text": "..."}]}
6-8 sections ordered by importance for tomorrow (right of way first). 4-7 points each.""", max_tokens=12000)
quiz = chat_json(SONNET, SYS, facts + """

Write 30 scenario multiple-choice questions testing the rules above as real city situations ("You are turning right at a green light and ...").
JSON list of {"id": "s1", "topic": "<Right of way|Turning|Lights|Pedestrians & cyclists|Buses & trams|Parking|Speed|Other>", "question": "...",
"options": ["4 options, exactly one correct"], "answer": <0-3, vary it>, "explanation": "<2-3 sentences citing the rule>"}""", max_tokens=14000)
rev = chat_json(HAIKU, "You are a strict German StVO fact-checker. Return ONLY JSON.", "Reference facts:\n" + facts +
  "\n\nFor each question: answer it yourself without looking at 'answer', and check it against the reference facts and current StVO. Return list of {\"id\":..., \"my_answer\": <index>, \"ok\": true|false, \"issues\": \"...\"}.\n" +
  json.dumps([{k: q[k] for k in ("id", "question", "options", "explanation")} for q in quiz], ensure_ascii=False), max_tokens=8000, temperature=0)
rv = {r["id"]: r for r in rev}
for q in quiz: q["review"] = rv.get(q["id"], {})
print("scenarios", len(quiz), "flagged", sum(1 for q in quiz if not q["review"].get("ok") or q["review"].get("my_answer") != q["answer"]))
json.dump({"guide": guide, "scenarios": quiz}, open(out, "w"), ensure_ascii=False, indent=1)
