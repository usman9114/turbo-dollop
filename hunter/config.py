"""All settings come from environment variables (or a .env file next to where you run the bot)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so the bot has no extra dependency. Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Where you live / pick up
    home_postal_code: str
    radius_km: int

    # Claude
    screen_model: str
    review_model: str
    use_claude: bool

    # Kleinanzeigen live search (Tier 1, opt-in)
    ka_live_enabled: bool
    ka_min_interval_s: float
    ka_daily_cap: int
    ka_block_hours: float
    chromium_path: str | None
    headless: bool

    # eBay Browse API
    ebay_client_id: str | None
    ebay_client_secret: str | None

    # IMAP (Kleinanzeigen saved-search alert emails)
    imap_host: str | None
    imap_user: str | None
    imap_password: str | None
    imap_folder: str

    # Telegram
    telegram_token: str | None
    telegram_chat_id: str | None

    # Economics (plan §4.2)
    vat_mode: str  # "25a" | "kleinunternehmer"
    sale_fee_pct: float
    ship_out_eur: float
    packaging_eur: float
    warranty_pct: float
    buy_fee_pct: float
    buy_fee_fixed: float
    ship_in_eur: float
    km_cost_eur: float
    vb_discount: float
    overhead_hours: float
    drive_kmh: float
    p_ok_cap: float
    min_ev: float
    min_ev_per_hour: float
    min_roi: float
    deep_review_ev: float

    db_path: Path

    @property
    def ebay_enabled(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def load() -> Settings:
    _load_dotenv()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return Settings(
        home_postal_code=os.environ.get("HUNTER_POSTAL_CODE", "80331"),
        radius_km=_i("HUNTER_RADIUS_KM", 50),
        screen_model=os.environ.get("HUNTER_SCREEN_MODEL", "claude-haiku-5-5"),
        review_model=os.environ.get("HUNTER_REVIEW_MODEL", "claude-sonnet-5-5"),
        use_claude=_b("HUNTER_USE_CLAUDE", has_key),
        ka_live_enabled=_b("HUNTER_KA_LIVE", False),
        ka_min_interval_s=_f("HUNTER_KA_MIN_INTERVAL_S", 30.0),
        ka_daily_cap=_i("HUNTER_KA_DAILY_CAP", 100),
        ka_block_hours=_f("HUNTER_KA_BLOCK_HOURS", 24.0),
        chromium_path=os.environ.get("HUNTER_CHROMIUM_PATH") or None,
        headless=_b("HUNTER_HEADLESS", True),
        ebay_client_id=os.environ.get("EBAY_CLIENT_ID") or None,
        ebay_client_secret=os.environ.get("EBAY_CLIENT_SECRET") or None,
        imap_host=os.environ.get("HUNTER_IMAP_HOST") or None,
        imap_user=os.environ.get("HUNTER_IMAP_USER") or None,
        imap_password=os.environ.get("HUNTER_IMAP_PASSWORD") or None,
        imap_folder=os.environ.get("HUNTER_IMAP_FOLDER", "INBOX"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        vat_mode=os.environ.get("HUNTER_VAT_MODE", "25a"),
        sale_fee_pct=_f("HUNTER_SALE_FEE_PCT", 0.05),
        ship_out_eur=_f("HUNTER_SHIP_OUT_EUR", 7.69),
        packaging_eur=_f("HUNTER_PACKAGING_EUR", 1.50),
        warranty_pct=_f("HUNTER_WARRANTY_PCT", 0.04),
        buy_fee_pct=_f("HUNTER_BUY_FEE_PCT", 0.045),
        buy_fee_fixed=_f("HUNTER_BUY_FEE_FIXED", 0.50),
        ship_in_eur=_f("HUNTER_SHIP_IN_EUR", 6.99),
        km_cost_eur=_f("HUNTER_KM_COST_EUR", 0.30),
        vb_discount=_f("HUNTER_VB_DISCOUNT", 0.90),
        overhead_hours=_f("HUNTER_OVERHEAD_HOURS", 0.5),
        drive_kmh=_f("HUNTER_DRIVE_KMH", 30.0),
        p_ok_cap=_f("HUNTER_P_OK_CAP", 0.9),
        min_ev=_f("HUNTER_MIN_EV", 40.0),
        min_ev_per_hour=_f("HUNTER_MIN_EV_PER_HOUR", 25.0),
        min_roi=_f("HUNTER_MIN_ROI", 0.40),
        deep_review_ev=_f("HUNTER_DEEP_REVIEW_EV", 40.0),
        db_path=Path(os.environ.get("HUNTER_DB", "hunter.sqlite3")),
    )
