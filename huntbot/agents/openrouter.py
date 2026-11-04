"""Minimal OpenRouter Chat Completions client (one HTTP request per `chat`; retries live in the role runner)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx2 as httpx

BASE = "https://openrouter.ai/api/v1"
# Documented Chat Completions fallback form (api_reference/overview: `models?: string[]; route?: 'fallback'`):
# the whole chain in priority order. "model+models" (primary in `model`, rest in `models`) is kept as an option.
FALLBACK_STYLE = "models"
DAILY_LIMITS = {"50", "1000"}


class OpenRouterError(RuntimeError):
    transient = False
    status: int | None = None

    def __init__(self, msg: str, status: int | None = None, transient: bool = False):
        super().__init__(msg)
        self.status, self.transient = status, transient


class RateLimited(OpenRouterError):
    def __init__(self, msg: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(msg, status=429)
        self.retry_after, self.daily = retry_after, daily


class ImageFetchError(OpenRouterError):
    pass


@dataclass
class Completion:
    text: str
    model: str
    finish_reason: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    status: int = 200


def forbidden_id(model_id: str) -> bool:
    """Moving targets and batch-only ids are never used."""
    return model_id.startswith("stealth/") or model_id.startswith("~") or model_id.endswith(":batch")


class OpenRouterClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, timeout: float = 60.0, allow_paid: bool = False,
                 app_url: str = "https://github.com/usman9114/turbo-dollop", app_title: str = "huntbot",
                 clock=time.time):
        self.api_key, self.allow_paid, self.app_url, self.app_title, self.clock = api_key, allow_paid, app_url, app_title, clock
        self.timeout = timeout
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "HTTP-Referer": self.app_url,
                "X-OpenRouter-Title": self.app_title, "X-Title": self.app_title, "Content-Type": "application/json"}

    def body(self, chain: list[str], messages: list[dict], *, schema: dict | None, schema_name: str,
             structured: Literal["json_schema", "json_object", "none"], temperature: float, max_tokens: int,
             reasoning: dict | None) -> dict:
        for m in chain:
            if forbidden_id(m):
                raise ValueError(f"model id {m!r} is not allowed (stealth/batch/moving alias)")
            if not self.allow_paid and not m.endswith(":free"):
                raise ValueError(f"refusing non-free model {m!r} (set HUNTBOT_OR_ALLOW_PAID=true or use the glm profile)")
        body: dict = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens, "seed": 7,
                      "usage": {"include": True}}
        if FALLBACK_STYLE == "model+models":
            body["model"] = chain[0]
            if len(chain) > 1:
                body["models"] = chain[1:]
        elif len(chain) == 1:
            body["model"] = chain[0]
        else:
            body["models"] = list(chain)
            body["route"] = "fallback"
        if reasoning is not None:
            body["reasoning"] = reasoning
        if structured == "json_schema" and schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        elif structured in ("json_object", "json_schema"):
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, chain: list[str], messages: list[dict], *, schema: dict | None = None, schema_name: str = "out",
             structured: Literal["json_schema", "json_object", "none"] = "none", temperature: float = 0.1,
             max_tokens: int = 2000, reasoning: dict | None = None, timeout: float | None = None) -> Completion:
        body = self.body(chain, messages, schema=schema, schema_name=schema_name, structured=structured,
                         temperature=temperature, max_tokens=max_tokens, reasoning=reasoning)
        t0 = time.monotonic()
        try:
            resp = self.http.post(f"{BASE}/chat/completions", json=body, headers=self._headers(),
                                  timeout=timeout or self.timeout)
        except httpx.TimeoutException as exc:
            raise OpenRouterError(f"timeout: {exc}", transient=True) from exc
        except httpx.TransportError as exc:
            raise OpenRouterError(f"transport error: {exc}", transient=True) from exc
        latency = int((time.monotonic() - t0) * 1000)
        status = resp.status_code
        try:
            data = resp.json()
        except Exception:
            data = {}
        err = data.get("error") if isinstance(data, dict) else None
        msg = str((err or {}).get("message", "") if isinstance(err, dict) else (err or ""))[:300] or resp.text[:200]

        if status == 200 and not err:
            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            if isinstance(text, list):    # content parts
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            return Completion(text=text, model=data.get("model", ""), finish_reason=choice.get("finish_reason") or "",
                              usage=data.get("usage") or {}, latency_ms=latency, status=200)
        if status == 200 and err:
            status = 502
        if status == 429:
            h = resp.headers
            retry = None
            try:
                retry = float(h["Retry-After"]) if "Retry-After" in h else None
            except ValueError:
                pass
            reset_s = None
            try:
                reset_s = float(h["X-RateLimit-Reset"]) / 1000.0 if "X-RateLimit-Reset" in h else None
            except ValueError:
                pass
            daily = ((h.get("X-RateLimit-Remaining") == "0" and reset_s is not None and reset_s - self.clock() > 120)
                     or h.get("X-RateLimit-Limit") in DAILY_LIMITS
                     or bool(re.search(r"per[- ]day|daily", msg, re.I)))
            raise RateLimited(msg or "rate limited", retry_after=retry, daily=daily)
        if status == 400:
            if "image" in msg.lower():
                raise ImageFetchError(msg, status=400)
            raise OpenRouterError(f"400: {msg}", status=400)
        if status in (401, 402, 403, 404):
            raise OpenRouterError(f"{status}: {msg}", status=status)
        raise OpenRouterError(f"{status}: {msg}", status=status, transient=True)

    def key_info(self) -> dict:
        resp = self.http.get(f"{BASE}/key", headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def list_models(self) -> list[dict]:
        resp = self.http.get(f"{BASE}/models", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", [])
