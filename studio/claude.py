"""Thin wrapper around the Anthropic SDK used by the planner and reviewer."""

from __future__ import annotations

import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

MODEL = os.environ.get("STUDIO_CLAUDE_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Reads ANTHROPIC_API_KEY (or an `ant auth login` profile) from the environment.
        _client = anthropic.Anthropic()
    return _client


class ClaudeRefused(RuntimeError):
    pass


def structured(
    *,
    system: str,
    content: list[dict] | str,
    schema: type[T],
    effort: str = "medium",
    max_tokens: int = 16000,
) -> T:
    """One Claude call that returns a validated Pydantic object."""
    response = client().beta.messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=schema,
        output_config={"effort": effort},
        # If a safety classifier declines, retry server-side on Anthropic's recommended fallback model.
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        details = response.stop_details
        raise ClaudeRefused(
            f"Claude declined ({details.category if details else 'unknown'}): "
            f"{details.explanation if details else ''}"
        )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens; ask for fewer shots per call")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output returned (request {response._request_id})")
    return response.parsed_output
