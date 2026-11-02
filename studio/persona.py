"""Persona definition: a fictional, clearly-adult AI character."""

from __future__ import annotations

import json
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
    face_image: str | None = Field(default=None, description="Chosen face portrait (from `studio faces`)")
    reference_images: list[ReferenceImage] = []

    @field_validator("age")
    @classmethod
    def _adult(cls, v: int) -> int:
        if v < MIN_AGE:
            raise ValueError(f"persona age must be {MIN_AGE}+ (got {v})")
        return v

    def resolved_reference(self, index: int, base_dir: Path) -> Path:
        return _resolve(self.reference_images[index].path, base_dir)

    def resolved_face(self, base_dir: Path) -> Path | None:
        return _resolve(self.face_image, base_dir) if self.face_image else None


def _resolve(p: str, base_dir: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (base_dir / path).resolve()


def references_index(persona_path: str | Path) -> Path:
    """Stills made by `studio stills` are recorded here, next to the persona YAML."""
    path = Path(persona_path)
    return path.with_name(f"{path.stem}.references.json")


def load_persona(path: str | Path) -> tuple[Persona, Path]:
    """Return the persona (YAML + generated stills) and the directory relative paths resolve against."""
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    persona = Persona.model_validate(data)
    index = references_index(path)
    if index.exists():
        extra = [ReferenceImage.model_validate(r) for r in json.loads(index.read_text())]
        persona.reference_images.extend(extra)
    return persona, path.parent.resolve()


def add_references(persona_path: str | Path, refs: list[ReferenceImage]) -> None:
    index = references_index(persona_path)
    existing = json.loads(index.read_text()) if index.exists() else []
    existing.extend(r.model_dump() for r in refs)
    index.write_text(json.dumps(existing, indent=2))
