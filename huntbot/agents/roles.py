"""Role runner: cache -> live chain -> budget -> at most 2 HTTP attempts -> parsed output."""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel

from ..config import Settings
from .budget import Budget, BudgetExhausted
from .jsonparse import ParseError, parse
from .openrouter import ImageFetchError, OpenRouterClient, OpenRouterError, RateLimited
from .prompts import SCHEMAS
from .registry import Registry
from .schemas import InspectorOutput, ReaderOutput, SkepticOutput
from .store import PROMPT_VERSION, AgentStore

log = logging.getLogger(__name__)

REMINDER = "\n\nYour previous reply was not valid JSON for the schema. Reply with ONLY the JSON object."


@dataclass
class RoleSpec:
    name: str
    chain: list[str]
    temperature: float
    max_tokens: int
    reasoning: dict | None
    timeout: float
    output: type[BaseModel]
    prompt_version: str
    needs_vision: bool = False


def specs_from_settings(s: Settings, chains: dict[str, list[str]] | None = None) -> dict[str, RoleSpec]:
    c = chains or {r: list(s.team_chain(r)) for r in ("reader", "inspector", "skeptic")}
    return {
        "reader": RoleSpec("reader", c["reader"], 0.1, 3000, {"effort": "low", "exclude": True}, 60,
                           ReaderOutput, PROMPT_VERSION["reader"]),
        "inspector": RoleSpec("inspector", c["inspector"], 0.1, 2000, {"effort": "low", "exclude": True}, 90,
                              InspectorOutput, PROMPT_VERSION["inspector"], needs_vision=True),
        "skeptic": RoleSpec("skeptic", c["skeptic"], 0.3, 6000, {"effort": "medium", "exclude": True}, 150,
                            SkepticOutput, PROMPT_VERSION["skeptic"]),
    }


class RoleRunner:
    def __init__(self, client: OpenRouterClient, budget: Budget, store: AgentStore, registry: Registry,
                 sleep=time.sleep):
        self.client, self.budget, self.store, self.registry, self.sleep = client, budget, store, registry, sleep
        self.fatal: str = ""               # set on 401/402/403: the team disables itself for this run
        self.errors: list[str] = []
        self.calls = 0

    def _note(self, msg: str) -> None:
        self.errors = (self.errors + [msg])[-10:]

    def run(self, spec: RoleSpec, messages: list[dict], cache_hash: str, purpose: str, listing_key: str,
            ) -> tuple[BaseModel | None, str]:
        cached = self.store.get_output(cache_hash, spec.name, spec.prompt_version)
        if cached:
            try:
                return spec.output.model_validate_json(cached[0]), cached[1]
            except Exception:
                pass
        chain = self.registry.live_chain(spec.chain, spec.needs_vision)
        if not chain:
            self._note(f"{spec.name}: no live model in chain")
            return None, ""
        chain = self.budget.affordable_chain(chain, purpose, allow_paid=self.client.allow_paid)

        msgs = messages
        for attempt in (1, 2):
            if attempt == 2:
                chain = self.budget.affordable_chain(chain, purpose, allow_paid=self.client.allow_paid)
            self.budget.wait_for_slot()
            try:
                comp = self.client.chat(
                    chain, msgs, schema=SCHEMAS[spec.name], schema_name=spec.name,
                    structured=self.registry.structured(chain[0]), temperature=spec.temperature,
                    max_tokens=spec.max_tokens, reasoning=spec.reasoning, timeout=spec.timeout)
            except RateLimited as exc:
                self._rec(spec, purpose, chain, "", 429, False, str(exc), 0, {}, listing_key)
                if exc.daily:
                    self.budget.mark_exhausted()
                    chain = self.budget.affordable_chain(chain, purpose, allow_paid=self.client.allow_paid)  # may raise
                elif exc.retry_after is not None and exc.retry_after > 30:
                    self._note(f"{spec.name}: rate limited, retry-after {exc.retry_after:.0f}s")
                    return None, ""
                else:
                    self.sleep(exc.retry_after if exc.retry_after is not None else 10)
                continue
            except ImageFetchError as exc:
                self._rec(spec, purpose, chain, "", 400, False, str(exc), 0, {}, listing_key)
                self._note(f"{spec.name}: image fetch failed")
                return None, ""
            except ValueError as exc:       # forbidden / non-free model id in a configured chain
                self.fatal = f"bad model configuration: {exc}"
                self._note(self.fatal)
                return None, ""
            except OpenRouterError as exc:
                self._rec(spec, purpose, chain, "", exc.status, False, str(exc), 0, {}, listing_key)
                self._note(f"{spec.name}: {exc}")
                if exc.status in (401, 402, 403):
                    self.fatal = f"OpenRouter {exc.status}: {exc}"
                    return None, ""
                if exc.transient and attempt == 1:
                    self.sleep(3)
                    continue
                return None, ""
            cost = comp.usage.get("cost")
            if cost is None:
                cost = self.registry.estimate_cost(comp.model or chain[0], comp.usage)
            try:
                out = parse(comp.text, spec.output)
            except ParseError as exc:
                self._rec(spec, purpose, chain, comp.model, 200, False, f"parse: {exc}", comp.latency_ms, comp.usage,
                          listing_key, float(cost or 0))
                self._note(f"{spec.name}: invalid JSON from {comp.model}")
                if attempt == 1:
                    chain = chain[1:] + chain[:1]
                    msgs = _with_reminder(messages)
                    continue
                return None, ""
            self._rec(spec, purpose, chain, comp.model, 200, True, "", comp.latency_ms, comp.usage, listing_key, float(cost or 0))
            self.store.put_output(cache_hash, spec.name, spec.prompt_version, comp.model, out.model_dump_json())
            return out, comp.model
        return None, ""

    def _rec(self, spec, purpose, chain, served, status, ok, error, latency, usage, key, cost=0.0) -> None:
        self.calls += 1
        self.budget.record(role=spec.name, purpose=purpose, models=chain, served=served, status=status, ok=ok,
                           error=error, latency_ms=latency, usage=usage, listing_key=key, cost_usd=cost)


def _with_reminder(messages: list[dict]) -> list[dict]:
    out = copy.deepcopy(messages)
    last = out[-1]
    if isinstance(last["content"], str):
        last["content"] += REMINDER
    else:
        last["content"].append({"type": "text", "text": REMINDER.strip()})
    return out
