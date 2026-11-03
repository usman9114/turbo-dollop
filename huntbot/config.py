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
    def use_llm(self) -> bool:
        return self.llm_enabled and self.anthropic_key_present


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
