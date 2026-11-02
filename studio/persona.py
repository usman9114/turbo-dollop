"""Persona definition: a fictional, clearly-adult AI character."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

MIN_AGE = 21


class ReferenceImage(BaseModel):
    """A still of the persona (e.g. from your Flux/SDXL + LoRA pipeline) for Wan image-to-video."""

    path: str
    description: str = Field(description="Outfit, setting and pose in this still")


class Persona(BaseModel):
    name: str
    handle: str
    age: int
    niche: str
    look: str = Field(description="Consistent physical description used in every prompt")
    personality: str
    voice: str = Field(description="How captions sound")
    wardrobe: list[str] = []
    settings: list[str] = []
    content_pillars: list[str] = []
    off_limits: list[str] = []
    reference_images: list[ReferenceImage] = []

    @field_validator("age")
    @classmethod
    def _adult(cls, v: int) -> int:
        if v < MIN_AGE:
            raise ValueError(f"persona age must be {MIN_AGE}+ (got {v})")
        return v

    def resolved_reference(self, index: int, base_dir: Path) -> Path:
        ref = self.reference_images[index]
        p = Path(ref.path)
        return p if p.is_absolute() else (base_dir / p).resolve()


def load_persona(path: str | Path) -> tuple[Persona, Path]:
    """Return the persona and the directory its relative paths resolve against."""
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    return Persona.model_validate(data), path.parent.resolve()
