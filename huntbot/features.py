"""Spec extraction from German listing text → model features.

Works for devices that are NOT in the catalog: CPU generation gives the release year (= age),
RAM/SSD give the spec, the product line gives the price tier.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import asdict, dataclass

THIS_YEAR = dt.date.today().year

# Product lines → tier. Order matters: first match wins (specific before generic).
LINES: list[tuple[str, str]] = [
    ("thinkpad_x1", r"\bx1\s*(carbon|yoga|extreme|nano)?\b"),
    ("thinkpad_p", r"\bthinkpad\s+p\d{2}"),
    ("thinkpad_t", r"\bthinkpad\s+t\d{2,3}|\bt(4[4-9]0|14|15|16)s?\b"),
    ("thinkpad_x", r"\bthinkpad\s+x\d{2,3}|\bx(2[4-9]0|3[89]0|13)\b"),
    ("thinkpad_l", r"\bthinkpad\s+l\d{2,3}|\bl(4[6-9]0|14|15)\b"),
    ("thinkpad_e", r"\bthinkpad\s+e\d{2,3}|\be(4[6-9]\d|5[89]\d|14|15)\b"),
    ("thinkpad_other", r"\bthinkpad\b"),
    ("latitude_7", r"latitude\s*7\d{3}"),
    ("latitude_5", r"latitude\s*5\d{3}"),
    ("latitude_3", r"latitude\s*3\d{3}"),
    ("elitebook", r"elitebook"),
    ("probook", r"probook"),
    ("macbook_pro", r"macbook\s*pro"),
    ("macbook_air", r"macbook\s*air"),
    ("surface", r"surface\s*(pro|laptop)"),
    ("dyson_v15", r"dyson.*\bv\s?15\b"),
    ("dyson_v11", r"dyson.*\bv\s?11\b"),
    ("dyson_v8", r"dyson.*\bv\s?8\b"),
    ("laptop_generic", r"\b(laptop|notebook)\b"),
]

# Release year of a CPU generation (Intel mobile) / Ryzen mobile series / Apple silicon.
INTEL_GEN_YEAR = {4: 2013, 5: 2015, 6: 2015, 7: 2016, 8: 2018, 9: 2019, 10: 2020, 11: 2021, 12: 2022, 13: 2023, 14: 2024}
RYZEN_SERIES_YEAR = {2: 2018, 3: 2019, 4: 2020, 5: 2021, 6: 2022, 7: 2023, 8: 2024}
APPLE_YEAR = {"m1": 2020, "m2": 2022, "m3": 2023, "m4": 2024}
# Fallback: approximate year from a known model number when no CPU is named.
MODEL_YEAR = [
    (r"\bt4[4]0s?\b", 2014), (r"\bt4[5]0s?\b", 2015), (r"\bt4[6]0s?\b", 2016), (r"\bt4[7]0s?\b", 2017),
    (r"\bt480s?\b", 2018), (r"\bt490s?\b", 2019), (r"\bt14s?\b.*(gen\s?1|g1)", 2020), (r"\bt14s?\b.*(gen\s?2|g2)", 2021),
    (r"\bt14s?\b.*(gen\s?3|g3)", 2022), (r"\bt14s?\b.*(gen\s?4|g4)", 2023), (r"\bt14s?\b", 2021),
    (r"\bx2[6]0\b", 2016), (r"\bx2[7]0\b", 2017), (r"\bx280\b", 2018), (r"\bx390\b", 2019), (r"\bx13\b", 2020),
    (r"\be4[78]0\b", 2017), (r"\be480\b", 2018), (r"\be490\b", 2019), (r"\be14\b", 2020),
    (r"\bl4[78]0\b", 2017), (r"\bl480\b", 2018), (r"\bl490\b", 2019), (r"\bl14\b", 2020),
    (r"x1\s*carbon.*(5th|g5|gen\s?5)", 2017), (r"x1\s*carbon.*(6th|g6|gen\s?6)", 2018),
    (r"x1\s*carbon.*(7th|g7|gen\s?7)", 2019), (r"x1\s*carbon.*(8th|g8|gen\s?8)", 2020),
    (r"x1\s*carbon.*(9th|g9|gen\s?9)", 2021), (r"x1\s*yoga.*(4th|g4|gen\s?4)", 2019),
    (r"latitude\s*\d(\d)(\d)0\b", None),   # Dell: 5400→2019, 5410→2020 … handled in code
    (r"\b(r5\d|r6\d|t[46]\d|z6\d|x[36]\d)\b", 2006),    # IBM-era ThinkPads
    (r"dyson.*\bv\s?8\b", 2016), (r"dyson.*\bv\s?11\b", 2019), (r"dyson.*\bv\s?15\b", 2021),
]

CONDITION_PATTERNS = [
    ("new", r"\b(neu\s+(und\s+)?ovp|originalverpackt|ungeöffnet|neu,?\s+unbenutzt|brandneu)\b"),
    ("defect", r"defekt|bastler|ersatzteil|kaputt|zum\s+ausschlachten|for\s+parts|not\s+working|startet\s+nicht|geht\s+nicht"),
    ("like_new", r"\b(neuwertig|wie\s+neu|top\s+zustand|sehr\s+gut(er)?\s+zustand|kaum\s+benutzt|neuzustand)\b"),
    ("worn", r"gebrauchsspuren|kratzer|delle|abgenutzt|risse?\b|verfärb"),
]


@dataclass
class Features:
    line: str
    year: int | None
    ram_gb: float | None
    ssd_gb: float | None
    condition: str            # new | like_new | good | worn | defect
    charger: bool | None
    platform: str             # kleinanzeigen | ebay
    price_kind: str           # ask | sold
    negotiable: bool

    @property
    def age(self) -> float | None:
        return None if self.year is None else max(0.0, THIS_YEAR - self.year)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["age"] = self.age
        return d


def _first_year(text: str) -> int | None:
    # Intel Core iX-NNNN: 8350U → 8th gen, 10210U / 1065G7 → 10th, 1135G7 → 11th, 1245U → 12th
    for m in re.finditer(r"\bi[3579][\s-]?(\d{4,5})[a-z]{0,2}\d?\b", text):
        digits = m.group(1)
        gen = int(digits[:2]) if digits[0] == "1" else int(digits[0])
        if gen in INTEL_GEN_YEAR:
            return INTEL_GEN_YEAR[gen]
    # "i5 8th Gen", "i5 11. Gen", "8. Generation Intel"
    m = re.search(r"\bi[3579][\s-]*(\d{1,2})(?:\.|th|te|ten)?\s*gen", text) or \
        re.search(r"\b(\d{1,2})(?:\.|th|te|ten)?\s*gen(?:eration)?\s*(?:intel|core|i[3579])", text)
    if m and int(m.group(1)) in INTEL_GEN_YEAR:
        return INTEL_GEN_YEAR[int(m.group(1))]
    if m := re.search(r"ryzen\s*[3579]\s*(?:pro\s*)?(\d)\d{3}u", text):
        return RYZEN_SERIES_YEAR.get(int(m.group(1)))
    if m := re.search(r"\b(m[1-4])(?:\s*(pro|max|chip))?\b", text):
        if "macbook" in text or "apple" in text:
            return APPLE_YEAR[m.group(1)]
    if m := re.search(r"(?:elitebook|probook|zbook)\s*\w*\s*\d{3,4}\w?\s*g\s?(\d{1,2})\b", text):
        return 2013 + int(m.group(1))   # HP: G5 = 2018 … G10 = 2023
    for pattern, year in MODEL_YEAR:
        if mm := re.search(pattern, text):
            if year is None:   # Dell Latitude x4y0: 5400→2019, 5410→2020, 5420→2021, 5430→2022
                return 2019 + int(mm.group(2)) if mm.group(1) == "4" else None
            return year
    return None


def _gb(text: str, kind: str) -> float | None:
    if kind == "ram":
        m = re.search(r"(\d{1,2})\s?gb\s*(?:ddr\d\s*)?(?:ram|arbeitsspeicher|ddr)", text) or \
            re.search(r"(?:ram|arbeitsspeicher)\s*:?\s*(\d{1,2})\s?gb", text) or \
            re.search(r"\b(\d{1,2})\s?/\s?\d{3,4}\s?gb\b", text) or re.search(r"\b(\d{1,2})\s?gb\b", text)
        val = float(m.group(1)) if m else None
        return val if val and 2 <= val <= 64 else None
    m = re.search(r"(\d(?:[.,]\d)?)\s?tb\b", text)
    if m:
        return float(m.group(1).replace(",", ".")) * 1024
    m = re.search(r"(\d{3,4})\s?gb\s*(?:m\.?2\s*)?(?:nvme\s*)?(?:ssd|festplatte|hdd|speicher|m\.2)", text) or \
        re.search(r"(?:ssd|festplatte)\s*:?\s*(\d{3,4})\s?gb", text) or re.search(r"\b\d{1,2}\s?/\s?(\d{3,4})\s?gb\b", text) or \
        re.search(r"\b(128|256|500|512|1000|1024|2048)\s?gb\b", text)
    val = float(m.group(1)) if m else None
    return val if val and 64 <= val <= 8192 else None


def _condition(text: str) -> str:
    if re.search(r"(kein|nicht|ohne)\s+(defekt|kaputt)", text):
        text = re.sub(r"(kein|nicht|ohne)\s+(defekt|kaputt)", " ", text)
    for name, pattern in CONDITION_PATTERNS:
        if re.search(pattern, text):
            return name
    return "good"


def extract(title: str, description: str = "", platform: str = "kleinanzeigen", price_kind: str = "ask",
            negotiable: bool = False, condition_field: str = "") -> Features:
    text = f"{title} {description} {condition_field}".lower().replace("thinkpad", "thinkpad ")
    text = re.sub(r"\s+", " ", text)
    line = next((name for name, pattern in LINES if re.search(pattern, text)), "other")
    charger = None
    if re.search(r"ohne\s+(netzteil|ladegerät|ladekabel)|netzteil\s+fehlt|kein\s+(netzteil|ladegerät)", text):
        charger = False
    elif re.search(r"(mit|inkl\.?|inklusive|samt|\+)\s+(\w+\s+){0,2}(netzteil|ladegerät|ladekabel|netzgerät)", text):
        charger = True
    condition = _condition(text)
    if platform == "ebay" and re.search(r"ersatzteil|defekt|for parts", condition_field.lower()):
        condition = "defect"
    return Features(
        line=line, year=_first_year(text), ram_gb=_gb(text, "ram"), ssd_gb=_gb(text, "ssd"),
        condition=condition, charger=charger, platform=platform, price_kind=price_kind, negotiable=negotiable,
    )


def log2_or(value: float | None, default: float) -> float:
    return math.log2(value) if value else math.log2(default)
