# Schilderwald Trainer: plan and how it was built

Goal: a small MCQ app for German road signs (50 new signs per week) plus a city-driving prep guide
for a first 2 × 45 min city lesson on 6 Nov 2026.

## Sources
- Sign images: official StVO pictograms on Wikimedia Commons (`Zeichen <nr> - <official name>, StVO <year>.svg`).
  German official signs are public domain (§5 UrhG). Commons was rate-limiting this machine, so most SVGs were
  fetched from the GitHub mirror `710Deckel/vz-katalog` (same Commons files, same names) via jsDelivr.
- Rules: StVO (gesetze-im-internet.de/stvo_2013), as amended 2020. `pipeline/city_facts.md` is the vetted fact sheet.

## Pipeline (OpenRouter helper models)
| Step | Model | Script |
|---|---|---|
| Pick 150 signs (3 weeks × 50, city-first) and resolve official names from Commons | Opus (orchestrator) | `signs.py`/`resolve.py` (scratch) |
| Write 1 MCQ + explanation + in-car tip per sign | `anthropic/claude-sonnet-5.5` | `pipeline/gen_signs.py` |
| Blind-answer every question and fact-check it | `anthropic/claude-haiku-5.5` | `pipeline/gen_signs.py` |
| Lesson guide + 30 city scenarios from the fact sheet, then Haiku review | Sonnet 5.5 → Haiku 5.5 | `pipeline/gen_lesson.py` |
| Adjudicate the 18 Haiku flags, hand-check Week 1, verify every image | Opus | `pipeline/fixes.py` |
| Build single-file app with embedded SVGs | — | `pipeline/build.py` → `app/index.html` |

## Weeks
- Week 1 (from 5 Nov): priority, speed zones, stopping/parking, bike/bus lanes, warnings for city traffic.
- Week 2 (from 12 Nov): markings, more warnings, weight/size bans, overtaking, zones, motorway basics.
- Week 3 (from 19 Nov): supplementary plates (Zusatzzeichen), special lanes, direction and detour signs.
To add Week 4: append numbers to the sign list, run `gen_signs.py` on them, review flags, rebuild.
