"""Claude screening: Haiku 5.5 reads text + up to 4 photos; Sonnet 5.5 re-reviews promising
or uncertain listings with all photos. Output is constrained to the `Screen` JSON schema."""

from __future__ import annotations

import json
import logging

import anthropic

from . import catalog
from .models import Listing, Screen
from .privacy import strip_pii

log = logging.getLogger(__name__)

SYSTEM = """You screen second-hand listings for a repair-and-flip business in Munich that buys broken
business laptops (ThinkPad, Latitude, EliteBook) and Dyson cordless vacuums, repairs them, and resells them.
Listings are German. Judge from the text and photos only; do not invent facts.

Return:
- device_model: exact model and generation if identifiable, else "unknown".
- catalog_key: one key from the catalog below that matches the device, or "none".
- fault_summary: one short English sentence on what is wrong and the likely fix.
- fault_category: the dominant fault.
- p_ok: probability (0-1) that a competent tech restores it to sellable condition with the parts you price.
  Software/password/SSD faults are usually high; "doesn't turn on", liquid and mainboard faults are low.
- parts_cost_eur: gross cost of parts needed (e.g. NVMe SSD 45, battery 30-75, keyboard 30-45, used screen 30, new FHD screen 100).
- repair_hours: hands-on time including diagnosis, testing, photos and listing.
- red_flags: every flag that applies. activation_lock = iCloud/Google/FRP lock; mdm_or_autopilot = "managed by your organisation",
  Intune, Autopilot; bios_password = BIOS/supervisor password set; company_asset_tag = inventory sticker or company device
  without explanation; stock_photos = manufacturer/internet photos instead of the real item; not_the_device = box, bag,
  charger or single part only.
- confidence: how sure you are overall.
- questions_for_seller: up to 3 short German questions that would resolve the biggest uncertainties
  (e.g. BIOS-Passwort gesetzt? Foto vom Typenschild/Seriennummer? Rechnung vorhanden?).

Catalog keys:
{catalog}"""


def _schema() -> dict:
    schema = Screen.model_json_schema()

    def strip(node):
        if isinstance(node, dict):
            node.pop("title", None)
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)
    strip(schema)
    return schema


SCHEMA = _schema()


def _content(listing: Listing, max_images: int) -> list[dict]:
    blocks: list[dict] = [
        {"type": "image", "source": {"type": "url", "url": url}}
        for url in listing.image_urls[:max_images] if url.startswith("https://")
    ]
    price = "unknown" if listing.price is None else ("free" if listing.price == 0 else f"€{listing.price:.0f}")
    text = (
        f"Source: {listing.source}\nTitle: {listing.title}\nAsking price: {price}{' VB' if listing.negotiable else ''}\n"
        f"Condition field: {listing.condition or '-'}\nShipping offered: {listing.shipping}\n"
        f"Description:\n{strip_pii(listing.description)[:3000] or '(none)'}"
    )
    blocks.append({"type": "text", "text": text})
    return blocks


class Screener:
    def __init__(self, screen_model: str, review_model: str, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(max_retries=3)
        self.screen_model = screen_model
        self.review_model = review_model
        cat = "\n".join(f"- {e.key}: {e.name}" for e in catalog.load_catalog())
        self.system = SYSTEM.format(catalog=cat)

    def _call(self, model: str, listing: Listing, max_images: int, effort: str) -> Screen | None:
        kwargs = dict(
            model=model,
            max_tokens=4000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        use_beta = model.startswith(("claude-sonnet-5-5", "claude-opus-5", "claude-fable-5"))
        for images in (max_images, 0):  # retry text-only if an image URL can't be fetched
            messages = [{"role": "user", "content": _content(listing, images)}]
            try:
                if use_beta:
                    # Server-side refusal fallback (routes declined requests to another model).
                    response = self.client.beta.messages.create(
                        **kwargs, messages=messages,
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                    )
                else:
                    response = self.client.messages.create(**kwargs, messages=messages)
            except anthropic.BadRequestError as exc:
                if images and "image" in str(exc).lower():
                    log.info("image fetch failed for %s, retrying text-only", listing.key)
                    continue
                log.warning("Claude rejected request for %s: %s", listing.key, exc)
                return None
            except anthropic.RateLimitError:
                log.warning("rate limited; using rule-based screen for %s", listing.key)
                return None
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                log.warning("Claude call failed for %s: %s", listing.key, exc)
                return None

            if response.stop_reason in ("refusal", "max_tokens"):
                log.warning("no usable answer for %s (stop_reason=%s)", listing.key, response.stop_reason)
                return None
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                screen = Screen.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("invalid JSON from %s for %s: %s", model, listing.key, exc)
                return None
            return _clamp(screen)
        return None

    def screen(self, listing: Listing) -> Screen | None:
        return self._call(self.screen_model, listing, max_images=4, effort="low")

    def review(self, listing: Listing) -> Screen | None:
        return self._call(self.review_model, listing, max_images=10, effort="medium")


def _clamp(screen: Screen) -> Screen:
    return screen.model_copy(update={
        "p_ok": max(0.0, min(1.0, screen.p_ok)),
        "parts_cost_eur": max(0.0, min(500.0, screen.parts_cost_eur)),
        "repair_hours": max(0.25, min(10.0, screen.repair_hours)),
        "questions_for_seller": screen.questions_for_seller[:3],
    })
