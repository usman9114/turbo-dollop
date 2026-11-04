"""Model registry: capabilities, expiry and pricing from the live /models list (refreshed once per UTC day)."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from typing import Literal

from .store import AgentStore


@dataclass(frozen=True)
class Caps:
    vision: bool
    structured: Literal["json_schema", "json_object", "none"]
    expires: str | None = None
    prompt_usd: float = 0.0        # per token
    completion_usd: float = 0.0


STATIC_CAPS: dict[str, Caps] = {
    "z-ai/glm-5.3-flash": Caps(True, "json_schema", None, 0.0000000313, 0.0000000846),
    "z-ai/glm-4.7-flash": Caps(False, "json_schema", None, 0.0000000605, 0.0000004),
    "z-ai/glm-5.3": Caps(False, "json_schema", None, 0.00000008, 0.0000036),
    "google/gemma-4-31b-it:free": Caps(True, "json_object"),
    "google/gemma-4-26b-a4b-it:free": Caps(True, "json_object"),
    "nvidia/nemotron-3.5-super-vl:free": Caps(True, "json_schema"),
    "nvidia/nemotron-3-ultra-550b-a55b:free": Caps(False, "none"),
    "xingchen-agi/xing4.0-29b-a4b:free": Caps(False, "json_schema"),
}


def _caps_from_model(m: dict) -> Caps:
    params = m.get("supported_parameters") or []
    structured = "json_schema" if "structured_outputs" in params else ("json_object" if "response_format" in params else "none")
    pr = m.get("pricing") or {}

    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    return Caps(vision="image" in ((m.get("architecture") or {}).get("input_modalities") or []), structured=structured,
                expires=m.get("expiration_date"), prompt_usd=f(pr.get("prompt")), completion_usd=f(pr.get("completion")))


class Registry:
    def __init__(self, store: AgentStore, clock=time.time):
        self.store, self.clock = store, clock
        self._live: dict[str, Caps] | None = None
        raw = store.kv_get("or_models_json")
        if raw:
            try:
                self._live = {k: Caps(**v) for k, v in json.loads(raw).items()}
            except Exception:
                self._live = None

    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def refresh(self, client) -> None:
        if self.store.kv_get("or_models_day") == self.today() and self._live:
            return
        try:
            models = client.list_models()
        except Exception:
            return
        live = {m["id"]: _caps_from_model(m) for m in models if m.get("id")}
        if not live:
            return
        self._live = live
        keep = {k: v.__dict__ for k, v in live.items()}
        self.store.kv_set("or_models_json", json.dumps(keep))
        self.store.kv_set("or_models_day", self.today())
        self._live = {k: Caps(**v) for k, v in keep.items()}

    def caps(self, model_id: str) -> Caps | None:
        if self._live and model_id in self._live:
            return self._live[model_id]
        return STATIC_CAPS.get(model_id)

    def live_chain(self, chain: tuple[str, ...] | list[str], needs_vision: bool) -> list[str]:
        horizon = (dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%d")
        out = []
        for m in chain:
            if self._live is not None and m not in self._live:
                continue
            c = self.caps(m)
            if c is None:
                if needs_vision:
                    continue
            else:
                if c.expires and c.expires[:10] <= horizon:
                    continue
                if needs_vision and not c.vision:
                    continue
            out.append(m)
        return out

    def structured(self, model_id: str) -> Literal["json_schema", "json_object", "none"]:
        c = self.caps(model_id)
        return c.structured if c else "none"

    def estimate_cost(self, served_or_primary: str, usage: dict) -> float:
        c = self.caps(served_or_primary)
        if not c:
            return 0.0
        return (usage.get("prompt_tokens") or 0) * c.prompt_usd + (usage.get("completion_tokens") or 0) * c.completion_usd
