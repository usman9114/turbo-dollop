"""Claude looks at keyframes of each rendered clip and approves or rejects it before posting."""

from __future__ import annotations

import base64
from pathlib import Path

from pydantic import BaseModel, Field

from .claude import structured

SYSTEM = """\
You are the quality and policy gate for an AI-generated Instagram persona account.
You see 3 keyframes (start, middle, end) of a ~5 second clip plus its caption.

Reject the clip if ANY of these hold:
- The person could plausibly be read as under 18.
- Nudity, visible nipples/genitals, sexual acts, or content Instagram would remove.
- The caption lacks an AI disclosure, solicits, or is sexually explicit.
- It resembles a specific real person, or shows readable logos/brands.
- Obvious AI failure: melted or extra fingers/limbs, face distortion between frames,
  identity drift (looks like a different person in different frames), garbled text.
Otherwise approve. Be strict: a rejected clip costs a re-render; a bad post can cost the account.
"""


class Review(BaseModel):
    approved: bool
    appears_adult: bool
    instagram_safe: bool
    caption_ok: bool
    quality_score: int = Field(description="1-10 visual quality and appeal")
    issues: list[str]
    suggested_prompt_fix: str = Field(description="how to change the Wan prompt if rejected, else empty")


def _img(path: Path) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(path.read_bytes()).decode(),
        },
    }


def review_clip(keyframes: list[Path], caption: str, hashtags: list[str]) -> Review:
    content: list[dict] = [_img(p) for p in keyframes]
    content.append(
        {
            "type": "text",
            "text": f"Caption:\n{caption}\n\nHashtags: {' '.join(hashtags)}\n\nReview this clip.",
        }
    )
    return structured(system=SYSTEM, content=content, schema=Review, effort="low", max_tokens=4000)
