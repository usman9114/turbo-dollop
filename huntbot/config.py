"""Settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, no overriding of real env vars."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _int(name: str, default: int) -> int:
    value = _env(name)
    return int(float(value)) if value else default


def _opt_int(name: str) -> int | None:
    value = _env(name)
    return int(float(value)) if value else None


def _tuple(name: str) -> tuple[str, ...] | None:
    value = _env(name)
    return tuple(x.strip() for x in value.split(",") if x.strip()) if value else None


# Screening-team model chains (primary first, then OpenRouter fallbacks), per profile.
PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    "glm": {
        "reader": ("z-ai/glm-5.3-flash", "z-ai/glm-4.7-flash", "google/gemma-4-31b-it:free"),
        "inspector": ("z-ai/glm-5.3-flash", "nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free"),
        "skeptic": ("z-ai/glm-5.3", "nvidia/nemotron-3-ultra-550b-a55b:free", "xingchen-agi/xing4.0-29b-a4b:free"),
    },
    "free": {
        "reader": ("google/gemma-4-31b-it:free", "nvidia/nemotron-3.5-super-vl:free", "xingchen-agi/xing4.0-29b-a4b:free"),
        "inspector": ("nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free"),
        "skeptic": ("nvidia/nemotron-3-ultra-550b-a55b:free", "xingchen-agi/xing4.0-29b-a4b:free", "google/gemma-4-31b-it:free"),
    },
}


@dataclass
class Economics:
    """Constants of the EV formula (plan section 4.2). All amounts in EUR."""

    kleinunternehmer: bool = False          # if True, no §25a VAT on the margin
    vat_rate: float = 0.19
    warranty_reserve: float = 0.04          # share of resale price
    packaging: float = 1.50
    ship_out: float = 7.69                  # DHL parcel up to 5 kg (incl. typical label)
    ship_in: float = 6.99                   # when the seller ships to you
    sell_fee_rate: float = 0.05             # eBay commercial fee for used goods
    buy_fee_rate: float = 0.045             # Kleinanzeigen "Sicher bezahlen"
    buy_fee_fixed: float = 0.50
    km_cost: float = 0.30
    default_travel_km: float = 20.0         # round trip for local pickup
    vb_discount: float = 0.85               # expected buy price when ad says "VB"
    p_ok_cap: float = 0.90                  # until ~30 devices have been calibrated
    min_ev: float = 40.0
    min_ev_per_hour: float = 25.0
    min_roi: float = 0.40
    # Quick flips: working device, just clean / wipe / photos / list.
    flip_hours: float = 0.75
    flip_parts: float = 3.0                 # cleaning supplies
    flip_p_ok: float = 0.9                  # risk it isn't as described
    flip_min_ev: float = 30.0
    flip_min_discount: float = 0.25         # ≥25% under the typical asking price
    # Postcode prefixes reachable for pickup (Munich region). Outside them: shipping or skip.
    home_plz_prefixes: tuple[str, ...] = ("80", "81", "82", "83", "84", "85", "86")


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: Path("huntbot.db"))
    port: int = 8787
    token: str = ""

    anthropic_key_present: bool = False
    llm_enabled: bool = True
    screen_model: str = "claude-haiku-5-5"
    review_model: str = "claude-sonnet-5-5"

    # Screening team (OpenRouter). Defaults keep today's behaviour when no key is set.
    openrouter_key: str = ""
    screen_provider: str = "auto"            # auto | openrouter | anthropic | rules  (aliases: team, claude)
    agent_profile: str = "glm"               # glm | free
    team_reader_models: tuple[str, ...] | None = None      # None = from the profile
    team_inspector_models: tuple[str, ...] | None = None
    team_skeptic_models: tuple[str, ...] | None = None
    agent_daily_cap: int | None = None       # free-model requests/day (None = server limit - 3)
    agent_run_cap: int = 12                  # free-model requests per run
    agent_max_calls_run: int = 40            # all requests per run (paid + free)
    agent_max_usd_day: float = 0.50          # paid spend per UTC day
    agent_run_seconds: int = 300
    agent_reserve: int | None = None         # free requests kept for interactive use
    agent_max_wait_min: int = 45
    push_watch: bool = True
    team_enforce: bool = False               # False = shadow mode: verdicts shown, alerts as before
    or_allow_paid: bool = False
    or_timeout: float = 60.0

    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_zip: str = "80331"

    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    telegram_token: str = ""
    telegram_chat_id: str = ""

    ondemand_enabled: bool = False   # Tier 1 live Kleinanzeigen search (breaks ToS §5, opt-in)
    ondemand_pages: int = 1          # result pages fetched per hunt (each ≥30 s apart)

    location_slug: str = "muenchen"
    location_id: str = "6411"
    radius_km: int = 30

    economics: Economics = field(default_factory=Economics)

    @property
    def ebay_enabled(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_user and self.imap_password)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def resolved_provider(self) -> str:
        """rules | claude | team (team = OpenRouter screening team)."""
        if not self.llm_enabled:
            return "rules"
        p = {"openrouter": "team", "anthropic": "claude"}.get(self.screen_provider, self.screen_provider)
        if p == "auto":
            return "team" if self.openrouter_key else ("claude" if self.anthropic_key_present else "rules")
        if p == "team" and not self.openrouter_key:
            return "rules"
        if p == "claude" and not self.anthropic_key_present:
            return "rules"
        return p if p in ("team", "claude") else "rules"

    @property
    def use_llm(self) -> bool:
        return self.resolved_provider in ("claude", "team")

    def team_chain(self, role: str) -> tuple[str, ...]:
        override = getattr(self, f"team_{role}_models")
        return tuple(override) if override else PROFILES.get(self.agent_profile, PROFILES["glm"])[role]


def load_settings() -> Settings:
    load_dotenv()
    econ = Economics(
        kleinunternehmer=_bool("HUNTBOT_KLEINUNTERNEHMER", False),
        default_travel_km=_float("HUNTBOT_DEFAULT_TRAVEL_KM", 20.0),
        min_ev=_float("HUNTBOT_MIN_EV", 40.0),
        min_ev_per_hour=_float("HUNTBOT_MIN_EV_PER_HOUR", 25.0),
        min_roi=_float("HUNTBOT_MIN_ROI", 0.40),
        flip_min_ev=_float("HUNTBOT_FLIP_MIN_EV", 30.0),
        flip_min_discount=_float("HUNTBOT_FLIP_MIN_DISCOUNT", 0.25),
        home_plz_prefixes=tuple(p.strip() for p in _env("HUNTBOT_HOME_PLZ_PREFIXES", "80,81,82,83,84,85,86").split(",") if p.strip()),
    )
    return Settings(
        db_path=Path(_env("HUNTBOT_DB", "huntbot.db")),
        port=int(_env("HUNTBOT_PORT", "8787")),
        token=_env("HUNTBOT_TOKEN"),
        openrouter_key=_env("OPENROUTER_API_KEY"),
        screen_provider=_env("HUNTBOT_SCREEN_PROVIDER", "auto").lower(),
        agent_profile=_env("HUNTBOT_AGENT_PROFILE", "glm").lower(),
        team_reader_models=_tuple("HUNTBOT_TEAM_READER_MODELS"),
        team_inspector_models=_tuple("HUNTBOT_TEAM_INSPECTOR_MODELS"),
        team_skeptic_models=_tuple("HUNTBOT_TEAM_SKEPTIC_MODELS"),
        agent_daily_cap=_opt_int("HUNTBOT_AGENT_DAILY_CAP"),
        agent_run_cap=_int("HUNTBOT_AGENT_RUN_CAP", 12),
        agent_max_calls_run=_int("HUNTBOT_AGENT_MAX_CALLS_RUN", 40),
        agent_max_usd_day=_float("HUNTBOT_AGENT_MAX_USD_DAY", 0.50),
        agent_run_seconds=_int("HUNTBOT_AGENT_RUN_SECONDS", 300),
        agent_reserve=_opt_int("HUNTBOT_AGENT_RESERVE"),
        agent_max_wait_min=_int("HUNTBOT_AGENT_MAX_WAIT_MIN", 45),
        push_watch=_bool("HUNTBOT_PUSH_WATCH", True),
        team_enforce=_bool("HUNTBOT_TEAM_ENFORCE", False),
        or_allow_paid=_bool("HUNTBOT_OR_ALLOW_PAID", False),
        or_timeout=_float("HUNTBOT_OR_TIMEOUT", 60.0),
        anthropic_key_present=bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN")),
        llm_enabled=_bool("HUNTBOT_LLM_ENABLED", True),
        screen_model=_env("HUNTBOT_SCREEN_MODEL", "claude-haiku-5-5"),
        review_model=_env("HUNTBOT_REVIEW_MODEL", "claude-sonnet-5-5"),
        ebay_client_id=_env("EBAY_CLIENT_ID"),
        ebay_client_secret=_env("EBAY_CLIENT_SECRET"),
        ebay_zip=_env("EBAY_ZIP", "80331"),
        imap_host=_env("IMAP_HOST", "imap.gmail.com"),
        imap_user=_env("IMAP_USER"),
        imap_password=_env("IMAP_PASSWORD"),
        imap_folder=_env("IMAP_FOLDER", "INBOX"),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        ondemand_enabled=_bool("HUNTBOT_ONDEMAND", False),
        ondemand_pages=max(1, min(3, int(_env("HUNTBOT_ONDEMAND_PAGES", "1")))),
        location_slug=_env("HUNTBOT_HOME_LOCATION_SLUG", "muenchen"),
        location_id=_env("HUNTBOT_HOME_LOCATION_ID", "6411"),
        radius_km=int(_env("HUNTBOT_RADIUS_KM", "30")),
        economics=econ,
    )
