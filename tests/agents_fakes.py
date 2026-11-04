"""Offline helpers for the screening-team tests: a scripted OpenRouter (httpx MockTransport) and builders."""

from __future__ import annotations

import json

import httpx2 as httpx

from huntbot.agents.budget import Budget
from huntbot.agents.openrouter import OpenRouterClient
from huntbot.agents.registry import Registry
from huntbot.agents.schemas import DEFAULTS, InspectorOutput, ReaderOutput, SkepticOutput
from huntbot.agents.store import AgentStore
from huntbot.agents.team import ScreeningTeam
from huntbot.config import Settings
from huntbot.db import DB
from huntbot.models import Listing
from huntbot.pipeline import Pipeline

DESC = ("Verkaufe mein ThinkPad T14 Gen 2, Intel i5-1135G7, 16 GB RAM, 256 GB SSD, Windows 11. "
        "Funktioniert einwandfrei, Netzteil dabei. Privatverkauf.")
TITLE = "Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 256GB SSD"


def listing(price=150.0, ad_id="1", title=TITLE, desc=DESC, images=("https://img.example/1.jpg",), **kw) -> Listing:
    return Listing("kleinanzeigen", ad_id, title, f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}", price, False, desc,
                   "80331 München", kw.pop("shipping", False), None, list(images), **kw)


def reader_json(**over) -> dict:
    base = dict(DEFAULTS[ReaderOutput])
    base.update(item_type="device", item_type_evidence=["ThinkPad T14 Gen 2"], device_model="ThinkPad T14 Gen 2",
                cpu="i5-1135G7", generation="Gen 2", ram_gb=16, ssd_gb=256, working_status="working",
                working_evidence=["Funktioniert einwandfrei"], fault_category="none", condition="good",
                charger_included="yes", seller_type="private", seller_evidence=["Privatverkauf"],
                price_is_for_this_item=True, confidence="high",
                questions_for_seller=["Ist ein BIOS-Passwort gesetzt?"])
    base.update(over)
    return base


def inspector_json(**over) -> dict:
    base = dict(DEFAULTS[InspectorOutput])
    base.update(images_reviewed=1, photo_kind="real_device", matches_title="yes", screen_state="windows_login",
                confidence="high")
    base.update(over)
    return base


def skeptic_json(**over) -> dict:
    base = dict(DEFAULTS[SkepticOutput])
    base.update(verdict="no_blocker", objections=[])
    base.update(over)
    return base


def completion(payload, model="x/served") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"id": "gen-1", "model": model, "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 80, "cost": 0.0002}}


def primary(body: dict) -> str:
    """Model the request asks for first (single `model`, or head of the documented `models` fallback chain)."""
    return body.get("model") or body["models"][0]


class FakeOR:
    """Dispatches on the `ROLE: x` marker in the system prompt. Each role value is a dict/str (always),
    a callable(body) -> dict/str, or a list of items consumed in order (an httpx.Response is returned as is)."""

    def __init__(self, reader=None, inspector=None, skeptic=None, key_info=None, models=None):
        self.script = {"reader": reader if reader is not None else reader_json(),
                       "inspector": inspector if inspector is not None else inspector_json(),
                       "skeptic": skeptic if skeptic is not None else skeptic_json()}
        self.requests: list[dict] = []
        self.roles: list[str] = []
        self.headers: list[dict] = []
        self.key_info, self.models = key_info, models
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/key"):
            if self.key_info is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"data": self.key_info})
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"data": self.models or []})
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        role = system.split("ROLE: ", 1)[1].split("\n", 1)[0].strip()
        self.requests.append(body)
        self.roles.append(role)
        self.headers.append(dict(request.headers))
        item = self.script[role]
        if isinstance(item, list):
            item = item.pop(0)
        if callable(item):
            item = item(body)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=completion(item, primary(body)))

    @property
    def count(self) -> int:
        return len(self.requests)

    def client(self, allow_paid=True) -> OpenRouterClient:
        return OpenRouterClient("k", http=httpx.Client(transport=self.transport), allow_paid=allow_paid)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make(tmp_path, fake: FakeOR | None = None, clock: Clock | None = None, notified=None, db_name="t.db", **settings):
    fake = fake or FakeOR()
    clock = clock or Clock()
    settings.setdefault("openrouter_key", "k")
    settings.setdefault("team_enforce", True)
    s = Settings(**settings)
    db = DB(tmp_path / db_name)
    team = ScreeningTeam(s, db, fake.client(), clock=clock, sleep=clock.sleep)
    pipe = Pipeline(s, db, team=team, notifier=(notified.append if notified is not None else None))
    return pipe, team, fake, clock
