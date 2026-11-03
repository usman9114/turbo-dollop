"""MCP server: lets Claude Desktop / Claude Code use your local huntbot (DB, price model, eBay API).

  claude mcp add huntbot -- huntbot mcp            # Claude Code
  Claude Desktop: see README ("MCP server").

Read-mostly: it can search, price, score and record sold prices. It never contacts sellers.
Live Kleinanzeigen search is only available if HUNTBOT_ONDEMAND=true (same limits as the CLI).
"""

from __future__ import annotations

from dataclasses import replace

from mcp.server.mcpserver import MCPServer

from .config import load_settings
from .db import DB
from .features import extract
from .models import Listing
from .pipeline import Pipeline
from .pricemodel import can_price

INSTRUCTIONS = """Deal-finding tools for the German second-hand market (Kleinanzeigen and eBay.de are separate
markets: Kleinanzeigen = local/cash/cheaper, eBay = national/fees/higher). Use `hunt` for a query,
`estimate_price` to value a device, `score_listing` for a listing the user pasted, `top_deals` for the
best stored alerts. Prices are model estimates unless labelled SOLD; say so. Never contact sellers."""


def _compact(r) -> dict:
    s, sc = r.score, r.screen
    return {
        "title": r.listing.title, "url": r.listing.url, "platform": r.listing.source, "price": r.listing.price,
        "negotiable": r.listing.negotiable, "location": r.listing.location, "mode": s.mode,
        "expected_profit_eur": round(s.ev), "profit_per_hour": round(s.ev_per_hour), "max_buy_eur": s.max_buy,
        "sell_on": s.sell_on, "resale_eur": round(s.resale), "typical_price_eur": round(s.fair_ask) if s.fair_ask else None,
        "discount": round(s.discount, 2) if s.discount is not None else None, "buy_signal": s.alert,
        "rejected": s.rejected, "red_flags": sc.red_flags, "device": sc.device_model, "fault": sc.fault_summary,
        "why": s.reasons, "offer_draft": r.as_dict()["offer"],
    }


def build(settings=None, db: DB | None = None) -> MCPServer:
    settings = settings or load_settings()
    db = db or DB(settings.db_path)
    pipeline = Pipeline(settings, db)
    ebay = None
    if settings.ebay_enabled:
        from .sources.ebay import EbayClient
        ebay = EbayClient(settings.ebay_client_id, settings.ebay_client_secret, settings.ebay_zip)
    server = MCPServer("huntbot", instructions=INSTRUCTIONS)

    @server.tool()
    def hunt(query: str, live: bool = False, limit: int = 10) -> dict:
        """Search eBay (API), stored Kleinanzeigen listings (alert mails, userscript) and optionally one live
        Kleinanzeigen page, then rank by expected profit. Query syntax: 'thinkpad t14 bis 250 versand'."""
        live_src = None
        if live and settings.ondemand_enabled:
            from .sources.kleinanzeigen_live import LiveSearch
            live_src = LiveSearch(db)
        out = pipeline.hunt(query, ebay=ebay, llm_budget=5, live=live_src)
        return {
            "results": [_compact(r) for r in out["results"][: max(1, min(limit, 30))]],
            "kleinanzeigen_search_links": out["kleinanzeigen_links"],
            "ebay_sold_prices_link": out["ebay_sold_url"],
            "notes": out["errors"] + ([] if settings.ondemand_enabled or not live else
                                       ["live Kleinanzeigen search is disabled (HUNTBOT_ONDEMAND=false)"]),
        }

    @server.tool()
    def estimate_price(title: str, description: str = "", asking_price: float | None = None) -> dict:
        """Estimate typical asking and sold prices on Kleinanzeigen and eBay for a laptop/Dyson description."""
        f = extract(title, description)
        if not can_price(f):
            return {"error": "can't identify product line / generation", "features": f.as_dict()}
        m = pipeline.model
        est = {f"{p}_{k}": m.predict(replace(f, platform=p, price_kind=k)).as_dict()
               for p in ("kleinanzeigen", "ebay") for k in ("ask", "sold")}
        out = {"features": f.as_dict(), "estimates_eur": est}
        if asking_price:
            out["discount_vs_typical_kleinanzeigen_ask"] = round(1 - asking_price / est["kleinanzeigen_ask"]["price"], 3)
        return out

    @server.tool()
    def score_listing(title: str, price: float, description: str = "", location: str = "",
                      platform: str = "kleinanzeigen", negotiable: bool = False, shipping: bool = False,
                      url: str = "") -> dict:
        """Score one listing the user pasted: mode (flip/repair), expected profit, max buy, red flags, offer draft."""
        listing = Listing(source="ebay" if platform == "ebay" else "kleinanzeigen", ad_id=f"pasted-{abs(hash((title, price))) % 10**9}",
                          title=title, url=url or "pasted", price=price, negotiable=negotiable, description=description,
                          location=location, shipping=shipping, raw_source="mcp")
        return _compact(pipeline.process(listing, use_llm=True, notify=False))

    @server.tool()
    def top_deals(limit: int = 10, buy_signals_only: bool = True) -> list[dict]:
        """Best stored listings by expected profit."""
        return [{"title": l.title, "url": l.url, "price": l.price, "platform": l.source,
                 "expected_profit_eur": round(s["ev"]), "mode": s.get("mode"), "max_buy_eur": s["max_buy"],
                 "buy_signal": s["alert"]}
                for l, s in db.top(limit=max(1, min(limit, 50)), alerts_only=buy_signals_only)]

    @server.tool()
    def add_sold_price(title: str, price: float, platform: str = "ebay") -> str:
        """Record a real sold price (e.g. from eBay 'Verkaufte Artikel'); it trains the price model 3× as strongly as asks."""
        cid = db.add_comp(title, price, "ebay" if platform == "ebay" else "kleinanzeigen", "sold")
        pipeline.refit()
        return f"saved comp #{cid}; model retrained on {pipeline.model.n} rows"

    return server


def main() -> None:
    build().run("stdio")
