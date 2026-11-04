"""Local web app: search UI, JSON API, and the ingest endpoint for the browser userscript."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import reports
from .config import Settings
from .db import DB
from .pipeline import Pipeline
from .sources.kleinanzeigen import from_companion

STATIC = Path(__file__).parent / "static"


class IngestBody(BaseModel):
    page: str = "search"            # "search" (results list) or "ad" (single ad page)
    items: list[dict]


def create_app(settings: Settings, db: DB, pipeline: Pipeline, ebay=None, live=None) -> FastAPI:
    app = FastAPI(title="huntbot", docs_url="/docs")

    def check_token(token: str | None) -> None:
        if settings.token and not hmac.compare_digest(token or "", settings.token):
            raise HTTPException(status_code=401, detail="bad or missing X-Huntbot-Token")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict:
        return {
            "llm": settings.use_llm, "ebay": settings.ebay_enabled and ebay is not None, "live": live is not None,
            "imap": settings.imap_enabled, "telegram": settings.telegram_enabled,
            "provider": settings.resolved_provider, "team_enforce": settings.team_enforce,
        }

    @app.get("/api/hunt")
    def hunt(q: str, budget: int = 15, live_search: bool = False, x_huntbot_token: str | None = Header(default=None)) -> dict:
        check_token(x_huntbot_token)
        out = pipeline.hunt(q, ebay=ebay, llm_budget=max(0, min(budget, 50)), live=live if live_search else None)
        hq = out["query"]
        return {
            "query": {"keywords": hq.keywords, "max_price": hq.max_price, "min_price": hq.min_price,
                      "shipping_only": hq.shipping_only, "variants": hq.variants},
            "results": [r.as_dict() for r in out["results"]],
            "kleinanzeigen_links": out["kleinanzeigen_links"],
            "ebay_sold_url": out["ebay_sold_url"],
            "errors": out["errors"],
            "live": [{"url": l["url"], "count": l["count"]} for l in out["live"]],
        }

    @app.post("/api/ingest")
    def ingest(body: IngestBody, x_huntbot_token: str | None = Header(default=None)) -> dict:
        """Called by the userscript with listings visible on a Kleinanzeigen page you opened."""
        check_token(x_huntbot_token)
        listings = [l for l in (from_companion(i) for i in body.items[:100]) if l]
        if body.page == "ad":
            results = [pipeline.process(l, use_llm=True, purpose="interactive") for l in listings]
        else:
            results = pipeline.process_many(listings, llm_budget=5, purpose="interactive")
        return {"results": {r.listing.ad_id: _badge(r) for r in results}}

    @app.get("/api/top")
    def top(limit: int = 50, alerts_only: bool = False, x_huntbot_token: str | None = Header(default=None)) -> list[dict]:
        check_token(x_huntbot_token)
        return [{"key": l.key, "title": l.title, "url": l.url, "price": l.price, "score": s}
                for l, s in db.top(limit=limit, alerts_only=alerts_only)]

    @app.post("/api/listing/{key}/pass")
    def pass_listing(key: str, x_huntbot_token: str | None = Header(default=None)) -> dict:
        check_token(x_huntbot_token)
        db.set_status(key, "passed")
        return {"ok": True}

    @app.get("/api/ledger.csv", response_class=PlainTextResponse)
    def ledger_csv(x_huntbot_token: str | None = Header(default=None)) -> str:
        check_token(x_huntbot_token)
        return reports.ledger_csv(db, settings.economics)

    @app.get("/api/stats", response_class=PlainTextResponse)
    def stats(x_huntbot_token: str | None = Header(default=None)) -> str:
        check_token(x_huntbot_token)
        return reports.summary_text(db, settings.economics)

    return app


def _badge(r) -> dict:
    d = r.as_dict()
    return {
        "ev": d["score"]["ev"], "ev_per_hour": d["score"]["ev_per_hour"], "max_buy": d["score"]["max_buy"],
        "alert": d["score"]["alert"], "rejected": d["score"]["rejected"], "reasons": d["score"]["reasons"],
        "flags": d["screen"]["red_flags"], "model": d["screen"]["device_model"],
        "fault": d["screen"]["fault_summary"], "p_ok": d["score"]["p_ok"], "offer": d["offer"],
        "model_used": d["model_used"],
        "verdict": (d["verdict"] or {}).get("decision"),
        "verdict_reason": ((d["verdict"] or {}).get("reasons") or [""])[0],
    }
