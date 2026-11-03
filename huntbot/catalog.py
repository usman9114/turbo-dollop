"""Comparison-price table: which model is this, and what does it sell for repaired?

The seed numbers in huntbot/data/catalog.csv are ESTIMATES from the plan. Replace them with
your own eBay "sold items" medians (10+ results per model) before trusting the EV.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import os

CATALOG_PATH = Path(os.environ.get("HUNTBOT_CATALOG") or Path(__file__).parent / "data" / "catalog.csv")


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    pattern: re.Pattern
    resale_p50: float
    salvage: float
    repair_hours: float
    label: str


@lru_cache(maxsize=4)
def load_catalog(path: Path = CATALOG_PATH) -> tuple[CatalogEntry, ...]:
    entries = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            entries.append(CatalogEntry(
                key=row["key"],
                name=row["name"],
                pattern=re.compile(row["pattern"], re.IGNORECASE),
                resale_p50=float(row["resale_p50"]),
                salvage=float(row["salvage"]),
                repair_hours=float(row["repair_hours"]),
                label=row.get("label", "EST"),
            ))
    return tuple(entries)


def normalise(text: str) -> str:
    text = text.lower().replace("thinkpad", "thinkpad ").replace("-", " ")
    return re.sub(r"\s+", " ", text)


def match(text: str, catalog: tuple[CatalogEntry, ...] | None = None) -> CatalogEntry | None:
    """First catalog row whose pattern matches. Rows are ordered most-specific first."""
    haystack = normalise(text)
    for entry in catalog or load_catalog():
        if entry.pattern.search(haystack):
            return entry
    return None


def by_key(key: str) -> CatalogEntry | None:
    for entry in load_catalog():
        if entry.key == key:
            return entry
    return None
