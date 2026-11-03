"""Claude: turns your query into searches, screens listings, and drafts offers.

  * plan_query    - Haiku 5.5: plain-language request -> German search keywords + filters
  * screen        - Haiku 5.5: text + up to 4 photos -> Assessment (cheap, every listing)
  * deep_review   - Sonnet 5.5: all photos -> Review with an offer draft (only promising ones)

Seller names and phone numbers are never sent: only title, description, price,
area and photos of the item.
"""

from __future__ import annotations

import json
from typing import Literal

import anthropic
from pydantic import BaseModel

from hunter.assessment import Assessment, Review
from hunter.models import Listing
from hunter.prices import PriceTable

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class ScreeningFailed(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Query planning
# ---------------------------------------------------------------------------

class QueryPlan(BaseModel):
    summary: str  # one line: what you're hunting for
    kleinanzeigen_queries: list[str]  # 1-4 short German keyword searches
    ebay_query: str
    category: Literal["notebooks", "any"]
    max_price: float | None
    exclude_words: list[str]  # drop listings whose title contains these


PLANNER_SYSTEM = """You turn a reseller's request into searches for Kleinanzeigen.de and eBay.de.
The reseller buys broken or cheap electronics in Germany, repairs them and resells them.

Rules:
- kleinanzeigen_queries: 1-4 short German keyword searches (2-4 words each), as people
  actually write ads. Cover the fault vocabulary sellers use, e.g. "defekt", "bastler",
  "ersatzteile", "passwort vergessen", "startet nicht", "akku defekt", "display defekt".
  Don't repeat near-identical searches.
- ebay_query: one short keyword search (eBay's "for parts" filter is added separately).
- category: "notebooks" if every query is about laptops, otherwise "any".
- max_price: only if the request states a budget, else null.
- exclude_words: words that mark irrelevant ads for this request (e.g. "suche", "tausche",
  "nur tastatur", "hülle"). Keep it short.
"""


def plan_query(query: str, model: str) -> QueryPlan:
    resp = client().messages.parse(
        model=model,
        max_tokens=2000,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": query}],
        output_format=QueryPlan,
        output_config={"effort": "low"},
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ScreeningFailed(f"query planning failed ({resp.stop_reason})")
    return resp.parsed_output


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

SCREEN_SYSTEM = """You screen second-hand listings for a small repair-and-resell business in Germany
(mostly business laptops: ThinkPad, Latitude, EliteBook; also Dyson stick vacuums).
Listings are German. Assess each one honestly and conservatively.

Fill the fields like this:
- relevant: does this listing match what the user is hunting for (given below)? An
  accessory, spare part on its own, "suche ..." (wanted) ad or a different product is not relevant.
- model_key: the matching key from the price table below, or "unknown". Be exact about
  generation (a T14 Gen 2 is not a Gen 1). If the generation can't be told, use the
  cheapest plausible key.
- model_guess: model, CPU, RAM, storage as far as visible.
- fault_summary: what is wrong according to the text and photos.
- likely_fix: the most probable repair (e.g. "new NVMe SSD + Windows 11 install").
- parts_cost_eur: realistic gross parts cost in Germany for that fix (battery €29-75,
  keyboard €29-43, used screen ~€30, new FHD screen ~€100, NVMe 512 GB ~€45,
  Dyson original battery ~€115). 0 for software-only fixes.
- repair_hours: hands-on time for diagnosis, fix and testing (not travel or listing).
- p_ok: probability (0-1) that the likely fix makes it a sellable, working unit. Software
  and password issues are high; "no sign of life", board faults and liquid damage are low.
- red_flags: only flags supported by the text or photos:
  activation_lock (iCloud/Find My/Microsoft account lock), mdm_or_autopilot ("managed by
  your organisation", Autopilot, Intune), bios_password (supervisor/BIOS password,
  Computrace), stock_photos (no real photos of this unit), company_asset_tag (inventory
  or leasing sticker), no_charger, possibly_stolen (urgent + far below value + no history,
  removed serial, seller can't answer basics), mainboard_damage, liquid_damage,
  commercial_seller, not_a_device (accessory, part only, box, wanted ad).
- confidence: 0-1, how sure you are about model and fault.
- notes: one or two short sentences a buyer needs (e.g. "RAM soldered", "ask for BIOS photo").

Price table (repaired resale estimates):
{catalogue}
"""

REVIEW_EXTRA = """
This is a deep review of a listing that looked promising. Look at every photo carefully
(BIOS screen, stickers, serial label, damage, charger, screen condition).
Also fill:
- offer_eur: a fair opening offer in € that leaves room for the repair and resale
  (the bot's maximum buy price is given; stay at or below it).
- offer_message_de: a short, friendly German message the user can paste into the
  Kleinanzeigen chat (max ~600 characters). Make the offer, and ask the checks that matter
  before paying: is the Microsoft/Apple account signed out, is there a BIOS/supervisor
  password, can they send a photo of the serial-number label, is there a receipt or proof
  of purchase. No greeting with a name. Sign off with "Viele Grüße".
"""


def _listing_text(listing: Listing, query: str, extra: str = "") -> str:
    data = {
        "source": listing.source,
        "title": listing.title,
        "price": listing.price_text or listing.price_eur,
        "negotiable": listing.negotiable,
        "area": listing.location,
        "distance_km": listing.distance_km,
        "shipping_possible": listing.shipping_possible,
        "posted": listing.posted,
        "attributes": listing.attributes,
        "seller_type": listing.seller_type,
        "description": listing.description[:4000],
    }
    return f"User is hunting for: {query}\n\nListing:\n{json.dumps(data, ensure_ascii=False, indent=1)}{extra}"


def _content(listing: Listing, query: str, max_images: int, extra: str = "", with_images: bool = True) -> list[dict]:
    blocks: list[dict] = []
    if with_images:
        for url in listing.image_urls[:max_images]:
            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    blocks.append({"type": "text", "text": _listing_text(listing, query, extra)})
    return blocks


def _system(prices: PriceTable, extra: str = "") -> list[dict]:
    return [{
        "type": "text",
        "text": SCREEN_SYSTEM.format(catalogue=prices.catalogue()) + extra,
        "cache_control": {"type": "ephemeral"},
    }]


def screen(listing: Listing, query: str, prices: PriceTable, model: str, max_images: int = 4) -> Assessment:
    def call(with_images: bool):
        return client().messages.parse(
            model=model,
            max_tokens=4000,
            system=_system(prices),
            messages=[{"role": "user", "content": _content(listing, query, max_images, with_images=with_images)}],
            output_format=Assessment,
            output_config={"effort": "low"},
        )

    try:
        resp = call(with_images=True)
    except anthropic.BadRequestError:
        # Usually an image URL that couldn't be fetched: retry on text alone.
        resp = call(with_images=False)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ScreeningFailed(f"screening returned {resp.stop_reason}")
    return resp.parsed_output


def deep_review(listing: Listing, query: str, prices: PriceTable, model: str,
                max_buy: float | None, max_images: int = 12) -> Review:
    extra = f"\n\nBot's maximum buy price for this unit: €{max_buy:.0f}" if max_buy is not None else ""

    def call(with_images: bool):
        return client().beta.messages.parse(
            model=model,
            max_tokens=8000,
            system=_system(prices, REVIEW_EXTRA),
            messages=[{"role": "user", "content": _content(listing, query, max_images, extra, with_images)}],
            output_format=Review,
            output_config={"effort": "medium"},
            # If a safety classifier declines, the API retries on its recommended fallback model.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    try:
        resp = call(with_images=True)
    except anthropic.BadRequestError:
        resp = call(with_images=False)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ScreeningFailed(f"deep review returned {resp.stop_reason}")
    return resp.parsed_output
