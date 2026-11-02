"""Claude plans a batch of Reels: Wan 2.1 prompts + Instagram captions in the persona's voice."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .claude import structured
from .persona import Persona

SYSTEM = """\
You are the creative director for a fictional AI-generated Instagram persona.
You plan short vertical (9:16) video clips that are rendered by the Wan 2.1 video model,
and you write the captions that go with them.

Content rules (these protect the account; never break them):
- The persona is a fictional adult. Describe them as a grown woman/man in their stated age;
  never use words that suggest youth (girl, teen, petite schoolgirl, young-looking, etc.).
- Instagram-safe and non-explicit: glamour, fashion, fitness, swimwear, lingerie-as-fashion,
  boudoir mood, confident and flirty energy are fine. No nudity, no sexual acts, no
  focus on genitals, no fetish content, no solicitation ("DM me for pics", prices, etc.).
- Never reference or resemble a real, identifiable person or brand logo.
- Every caption must disclose the account is AI (e.g. "#AIgenerated" or "AI-made").
- Respect the persona's off-limits list.

Writing Wan 2.1 prompts:
- One continuous ~5 second shot. Describe subject, outfit, setting, lighting, a single
  clear motion, and camera movement in plain cinematic English (60-120 words).
- Wan responds well to concrete motion ("slowly turns her head toward the camera and smiles",
  "hair moves in the warm breeze", "camera slowly dollies in") and to lighting/lens terms.
- Avoid text, logos, mirrors with complex reflections, hands doing fine manipulation, and
  multiple people: these render badly.
- For mode "i2v" the reference still already fixes the face, outfit and setting: describe
  only the motion, expression, camera and atmosphere, consistent with that still.
- For mode "t2v" include the persona's full look description so the character stays consistent.

Captions: in the persona's voice, a hook in the first line, 1-3 short lines, then hashtags
separately. Make engagement prompts (questions, polls) rather than explicit teasing.
"""


class Shot(BaseModel):
    id: str = Field(description="short slug, e.g. 'golden-hour-balcony'")
    concept: str = Field(description="one-line idea and why it should perform")
    mode: Literal["i2v", "t2v"]
    reference_index: int = Field(description="index into reference_images for i2v, -1 for t2v")
    wan_prompt: str
    negative_prompt_extra: str = Field(description="shot-specific things to avoid, comma separated, may be empty")
    caption: str
    hashtags: list[str]
    alt_text: str
    cover_text: str = Field(description="2-5 word on-screen hook to add in editing, may be empty")


class ContentPlan(BaseModel):
    theme: str
    shots: list[Shot]


def plan_batch(persona: Persona, count: int, brief: str = "") -> ContentPlan:
    refs = [
        {"index": i, "description": r.description} for i, r in enumerate(persona.reference_images)
    ]
    mode_note = (
        "Reference stills are available: prefer mode 'i2v' and pick a matching reference_index."
        if refs
        else "No reference stills: use mode 't2v' with reference_index -1."
    )
    user = (
        f"Persona:\n{persona.model_dump_json(indent=2, exclude={'reference_images'})}\n\n"
        f"Reference stills:\n{json.dumps(refs, indent=2)}\n\n"
        f"{mode_note}\n\n"
        f"Plan {count} distinct clips for this week. Vary settings, outfits and hooks across the "
        f"content pillars.\n"
        + (f"Extra brief from the operator: {brief}\n" if brief else "")
    )
    plan = structured(system=SYSTEM, content=user, schema=ContentPlan, effort="medium")

    # Guard against out-of-range reference picks.
    for shot in plan.shots:
        if shot.mode == "i2v" and not (0 <= shot.reference_index < len(persona.reference_images)):
            shot.mode, shot.reference_index = "t2v", -1
    return plan
