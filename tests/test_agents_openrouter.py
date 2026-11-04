import httpx2 as httpx
import pytest

from huntbot.agents import prompts
from huntbot.agents.budget import BudgetExhausted
from huntbot.agents.openrouter import OpenRouterClient
from huntbot.agents.registry import Registry
from huntbot.agents.store import content_hash

from .agents_fakes import Clock, FakeOR, listing, make, primary, reader_json, inspector_json

MSG = [{"role": "system", "content": "ROLE: reader\n"}, {"role": "user", "content": "hi"}]


def body(chain, structured):
    return OpenRouterClient("k", allow_paid=True).body(chain, MSG, schema={"type": "object"}, schema_name="reader",
                                                      structured=structured, temperature=0.1, max_tokens=100, reasoning=None)


def test_request_body_and_headers(tmp_path):
    reg = Registry.__new__(Registry)
    reg._live = None
    chain = ["nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free"]
    b = body(chain, Registry.structured(reg, chain[0]))
    assert "model" not in b and b["models"] == chain and b["route"] == "fallback" and b["usage"] == {"include": True}
    single = body(chain[:1], Registry.structured(reg, chain[0]))
    assert single["model"] == chain[0] and "models" not in single
    assert b["response_format"]["type"] == "json_schema" and b["response_format"]["json_schema"]["strict"] is True
    assert body(["google/gemma-4-31b-it:free"], Registry.structured(reg, "google/gemma-4-31b-it:free"))["response_format"] == {"type": "json_object"}
    assert "response_format" not in body(["nvidia/nemotron-3-ultra-550b-a55b:free"], Registry.structured(reg, "nvidia/nemotron-3-ultra-550b-a55b:free"))
    fake = FakeOR()
    fake.client().chat(chain, MSG, structured="none")
    h = fake.headers[0]
    assert h["authorization"] == "Bearer k" and h["x-openrouter-title"] == "huntbot"
    assert h["http-referer"] == "https://github.com/usman9114/turbo-dollop"


def test_paid_and_moving_ids_refused():
    c = OpenRouterClient("k", allow_paid=False)
    with pytest.raises(ValueError):
        c.body(["z-ai/glm-5.3"], MSG, schema=None, schema_name="x", structured="none", temperature=0, max_tokens=1, reasoning=None)
    c.body(["a/b:free"], MSG, schema=None, schema_name="x", structured="none", temperature=0, max_tokens=1, reasoning=None)
    paid = OpenRouterClient("k", allow_paid=True)
    for bad in ("stealth/crouton-alpha", "~anthropic/claude-sonnet-latest", "z-ai/glm-5.3-flash:batch"):
        with pytest.raises(ValueError):
            paid.body([bad], MSG, schema=None, schema_name="x", structured="none", temperature=0, max_tokens=1, reasoning=None)


def run_reader(team, l=None):
    l = l or listing()
    return team.runner.run(team.specs["reader"], prompts.reader_messages(l), content_hash("reader", l), "pipeline", l.key)


def calls(team):
    return [dict(r) for r in team.db.conn.execute("SELECT * FROM llm_calls ORDER BY id")]


def test_429_retry_after_sleeps_and_retries_once(tmp_path):
    limited = httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "slow down"}})
    pipe, team, fake, clock = make(tmp_path, FakeOR(reader=[limited, reader_json()]))
    out, model = run_reader(team)
    assert out is not None and 2 in clock.sleeps and fake.count == 2
    assert [c["http_status"] for c in calls(team)] == [429, 200]


def test_daily_429_exhausts_the_day(tmp_path):
    clock = Clock()
    daily = httpx.Response(429, headers={"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                         "X-RateLimit-Reset": str(int((clock.t + 6 * 3600) * 1000))},
                           json={"error": {"message": "Rate limit exceeded: free-models-per-day"}})
    pipe, team, fake, _ = make(tmp_path, FakeOR(reader=[daily]), clock, agent_profile="free")
    with pytest.raises(BudgetExhausted):
        run_reader(team)
    assert team.budget.exhausted_today() and fake.count == 1
    with pytest.raises(BudgetExhausted):
        run_reader(team, listing(ad_id="2", title="Other title ThinkPad"))
    assert fake.count == 1                              # no further HTTP calls that day


def test_503_then_200_and_double_timeout(tmp_path):
    pipe, team, fake, clock = make(tmp_path, FakeOR(reader=[httpx.Response(503, json={"error": {"message": "overloaded"}}), reader_json()]))
    out, _ = run_reader(team)
    assert out is not None and len(calls(team)) == 2 and 3 in clock.sleeps

    def boom(body):
        raise httpx.ReadTimeout("slow")
    pipe, team, fake, _ = make(tmp_path, FakeOR(reader=[boom, boom]), db_name="t2.db")
    out, _ = run_reader(team)
    assert out is None and fake.count == 2             # hard cap: 2 attempts per role call


def test_image_error_returns_none_without_retry(tmp_path):
    bad = httpx.Response(400, json={"error": {"message": "Failed to fetch image from URL"}})
    pipe, team, fake, _ = make(tmp_path, FakeOR(inspector=[bad]))
    l = listing()
    out, _ = team.runner.run(team.specs["inspector"], prompts.inspector_messages(l), content_hash("inspector", l), "pipeline", l.key)
    assert out is None and fake.count == 1


def test_invalid_json_retries_with_rotated_chain_and_reminder(tmp_path):
    pipe, team, fake, _ = make(tmp_path, FakeOR(reader=["sorry, no json here", reader_json()]))
    out, _ = run_reader(team)
    assert out is not None and fake.count == 2
    first, second = fake.requests
    chain = team.specs["reader"].chain
    assert primary(first) == chain[0] and primary(second) == chain[1]
    assert "not valid JSON" in second["messages"][-1]["content"]
    assert [c["ok"] for c in calls(team)] == [0, 1]


def test_inspector_message_has_image_parts(tmp_path):
    fake = FakeOR()
    pipe, team, *_ = make(tmp_path, fake)
    pipe.process(listing())
    content = fake.requests[1]["messages"][1]["content"]
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://img.example/1.jpg"}}
