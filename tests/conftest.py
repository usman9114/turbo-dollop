import pytest


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    """A developer's keys must never make tests touch the network."""
    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HUNTBOT_SCREEN_PROVIDER",
                 "HUNTBOT_AGENT_PROFILE", "HUNTBOT_TEAM_ENFORCE"):
        monkeypatch.delenv(name, raising=False)
