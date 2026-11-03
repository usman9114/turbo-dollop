"""Comparison-price table (prices.yaml) and model matching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).with_name("prices.yaml")


@dataclass
class ModelPrice:
    key: str
    category: str
    label: str
    resale_p50: float
    salvage: float
    repair_hours: float
    default_parts: float
    ship_out: float | None = None
    patterns: list[re.Pattern] = field(default_factory=list, repr=False)


class PriceTable:
    def __init__(self, models: list[ModelPrice]):
        self.models = models
        self.by_key = {m.key: m for m in models}

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "PriceTable":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        models: list[ModelPrice] = []
        for category, entries in data.items():
            for key, e in (entries or {}).items():
                models.append(
                    ModelPrice(
                        key=key,
                        category=category,
                        label=e.get("label", key),
                        resale_p50=float(e["resale_p50"]),
                        salvage=float(e.get("salvage", 0)),
                        repair_hours=float(e.get("repair_hours", 1.0)),
                        default_parts=float(e.get("default_parts", 0)),
                        ship_out=float(e["ship_out"]) if e.get("ship_out") is not None else None,
                        patterns=[re.compile(p, re.IGNORECASE) for p in e.get("match", [])],
                    )
                )
        return cls(models)

    def match(self, text: str) -> ModelPrice | None:
        """First model (in file order) whose pattern appears in the text."""
        for m in self.models:
            if any(p.search(text) for p in m.patterns):
                return m
        return None

    def get(self, key: str | None) -> ModelPrice | None:
        return self.by_key.get(key) if key else None

    def catalogue(self) -> str:
        """Compact list for the Claude prompt: key = label (resale)."""
        return "\n".join(f"- {m.key}: {m.label} (repaired ≈ €{m.resale_p50:.0f})" for m in self.models)
