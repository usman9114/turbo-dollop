"""Telegram deal cards + commands, via the plain Bot API (long polling).

Commands (only from TELEGRAM_CHAT_ID):
  /hunt <query>                    ranked results for a query, e.g. /hunt thinkpad t14 bis 150
  /top                             best open alerts
  /bought <listing-key|title> <€>  log a purchase  -> returns inventory id
  /cost <inv-id> <€> [note]        parts / other cost;  /cost 3 45 ssd
  /time <inv-id> <minutes>         labour time
  /sold <inv-id> <€> [fees] [ship] log a sale
  /stats                           profit summary
The bot never contacts sellers.
"""

from __future__ import annotations

import html
import logging
import time

import httpx2 as httpx

from . import reports
from .db import DB
from .offers import draft_offer
from .pipeline import Pipeline, Result

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.http = http or httpx.Client(timeout=40)

    def call(self, method: str, **payload) -> dict:
        resp = self.http.post(API.format(token=self.token, method=method), json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def send(self, text: str, **extra) -> dict:
        return self.call("sendMessage", chat_id=self.chat_id, text=text[:4096], parse_mode="HTML",
                         disable_web_page_preview=True, **extra)

    def send_card(self, result: Result) -> None:
        caption, markup = card(result)
        image = result.listing.image_urls[0] if result.listing.image_urls else None
        if image:
            try:
                self.call("sendPhoto", chat_id=self.chat_id, photo=image, caption=caption[:1024],
                          parse_mode="HTML", reply_markup=markup)
                return
            except RuntimeError as exc:
                log.info("photo send failed (%s), sending text card", exc)
        self.send(caption, reply_markup=markup)


def _eur(v) -> str:
    return "?" if v is None else f"€{v:.0f}"


def card(result: Result) -> tuple[str, dict]:
    l, sc, s = result.listing, result.score, result.screen
    price = "kostenlos" if l.price == 0 else _eur(l.price) + (" VB" if l.negotiable else "")
    flags = ", ".join(s.red_flags) if s.red_flags else "none"
    lines = [
        f"<b>{html.escape(l.title[:120])}</b>",
        f"{l.source} · {price} · {html.escape(l.location or '-')}",
        f"{'⚡ QUICK FLIP' if sc.mode == 'flip' else '🔧 REPAIR'}"
        + (f" · {sc.discount:.0%} under typical {_eur(sc.fair_ask)}" if sc.discount is not None else ""),
        f"🧮 <b>EV {_eur(sc.ev)}</b> · {_eur(sc.ev_per_hour)}/h · ROI {sc.roi:.0%} · max buy {_eur(sc.max_buy)}",
        f"💶 sell on {sc.sell_on} ~{_eur(sc.resale)}"
        + (f" (80%: {_eur(sc.price_low)}–{_eur(sc.price_high)} on eBay)" if sc.price_low else ""),
        f"📦 {html.escape(s.device_model)} — {html.escape(s.fault_summary[:160])}",
        f"p_ok {sc.p_ok:.2f} · parts {_eur(s.parts_cost_eur)} · {sc.hours:.1f} h · resale {_eur(sc.resale)}",
        f"🚩 {html.escape(flags)}",
        f"<i>{html.escape(result.model_used)} · {html.escape('; '.join(sc.reasons)[:200])}</i>",
    ]
    buttons = [[{"text": "Open ad", "url": l.url}]]
    offer = draft_offer(l, s, sc)
    row = []
    if offer:
        row.append({"text": "Copy offer", "copy_text": {"text": offer}})
    row.append({"text": "Pass", "callback_data": f"pass|{l.key}"[:64]})
    buttons.append(row)
    buttons.append([{"text": f"Bought @ {_eur(sc.buy_estimate)}", "callback_data": f"buy|{l.key}"[:64]}])
    return "\n".join(lines), {"inline_keyboard": buttons}


class Bot:
    """Long-polling command loop."""

    def __init__(self, tg: Telegram, pipeline: Pipeline, db: DB, ebay=None, live=None):
        self.tg, self.pipeline, self.db, self.ebay, self.live = tg, pipeline, db, ebay, live
        self.offset = 0

    def run_forever(self) -> None:
        log.info("Telegram bot polling")
        while True:
            try:
                updates = self.tg.call("getUpdates", offset=self.offset, timeout=30,
                                       allowed_updates=["message", "callback_query"])
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception as exc:
                    log.exception("update failed")
                    self.tg.send(f"⚠️ {html.escape(str(exc))}")

    def process_pending(self) -> int:
        """Answer commands that queued up since the last run (for cron / GitHub Actions)."""
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        row = self.db.conn.execute("SELECT v FROM kv WHERE k='tg_offset'").fetchone()
        self.offset = int(row["v"]) if row else 0
        updates = self.tg.call("getUpdates", offset=self.offset, timeout=0, allowed_updates=["message", "callback_query"])
        for update in updates:
            self.offset = update["update_id"] + 1
            try:
                self.handle(update)
            except Exception as exc:
                log.exception("update failed")
                self.tg.send(f"⚠️ {html.escape(str(exc))}")
            self.db.conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('tg_offset', ?)", (str(self.offset),))
            self.db.conn.commit()
        return len(updates)

    def handle(self, update: dict) -> None:
        if cq := update.get("callback_query"):
            if str(cq["message"]["chat"]["id"]) != self.tg.chat_id:
                return
            action, _, key = cq.get("data", "").partition("|")
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            if action == "pass":
                self.db.set_status(key, "passed")
                self.tg.send(f"Passed {html.escape(key)}")
            elif action == "buy":
                listing = self.db.get_listing(key)
                stored = self.db.get_score(key)
                if listing and stored:
                    price = stored[1].get("buy_estimate") or listing.price or 0
                    inv = self.db.add_purchase(listing.title, float(price), key)
                    self.tg.send(f"Logged purchase #{inv} at €{price:.0f}. Fix the price with /bought if it differed. "
                                 "Keep the receipt + ownership declaration.")
            return

        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.tg.chat_id:
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            text = "/hunt " + text  # plain text = a hunt query
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        handler = getattr(self, f"cmd_{cmd[1:]}", None)
        if handler is None:
            self.tg.send(html.escape(__doc__.split("Commands", 1)[1]))
            return
        handler(arg.strip())

    def cmd_start(self, arg: str) -> None:
        self.tg.send("Send me a query like <code>thinkpad t14 defekt bis 150</code>.")

    def cmd_help(self, arg: str) -> None:
        self.tg.send(html.escape(__doc__.split("Commands", 1)[1]))

    def cmd_hunt(self, arg: str) -> None:
        if not arg:
            self.tg.send("Usage: /hunt thinkpad t480 bis 120")
            return
        self.tg.send(f"🔎 Hunting <b>{html.escape(arg)}</b> …")
        out = self.pipeline.hunt(arg, ebay=self.ebay, llm_budget=10, live=self.live)
        results = [r for r in out["results"] if not r.score.rejected]
        for r in results[:5]:
            self.tg.send_card(r)
        links = "\n".join(f'• <a href="{html.escape(x["url"])}">{html.escape(x["phrase"])}</a>'
                          for x in out["kleinanzeigen_links"][:4])
        summary = (f"{len(out['results'])} listings scored, {sum(r.score.alert for r in results)} pass the buy rule.\n"
                   f"Kleinanzeigen searches (open in your browser with the userscript, or save as alerts):\n{links}\n"
                   f'<a href="{html.escape(out["ebay_sold_url"])}">eBay sold prices</a>')
        if out["errors"]:
            summary += "\n⚠️ " + html.escape("; ".join(out["errors"]))
        self.tg.send(summary)

    def cmd_top(self, arg: str) -> None:
        rows = self.db.top(limit=10, alerts_only=True)
        if not rows:
            self.tg.send("No open alerts.")
            return
        self.tg.send("\n".join(
            f'€{s["ev"]:.0f} EV · <a href="{html.escape(l.url)}">{html.escape(l.title[:60])}</a> · {_eur(l.price)}'
            for l, s in rows))

    def cmd_bought(self, arg: str) -> None:
        what, _, price_s = arg.rpartition(" ")
        if not what or not price_s.replace(",", ".").replace(".", "", 1).isdigit():
            self.tg.send("Usage: /bought <listing key or title> <price>")
            return
        price = float(price_s.replace(",", "."))
        listing = self.db.get_listing(what) if ":" in what else None
        name = listing.title if listing else what
        inv = self.db.add_purchase(name, price, listing.key if listing else None)
        self.tg.send(f"Purchase #{inv} logged: {html.escape(name)} €{price:.2f}")

    def cmd_cost(self, arg: str) -> None:
        inv, amount, *note = arg.split(" ", 2)
        self.db.add_cost(int(inv), "parts", float(amount.replace(",", ".")), note[0] if note else "")
        self.tg.send(f"Cost €{amount} added to #{inv}")

    def cmd_time(self, arg: str) -> None:
        inv, minutes = arg.split()
        self.db.add_cost(int(inv), "labour_min", float(minutes))
        self.tg.send(f"{minutes} min logged on #{inv}")

    def cmd_sold(self, arg: str) -> None:
        parts = arg.split()
        inv, price = int(parts[0]), float(parts[1].replace(",", "."))
        fees = float(parts[2]) if len(parts) > 2 else round(price * self.pipeline.econ.sell_fee_rate, 2)
        ship = float(parts[3]) if len(parts) > 3 else self.pipeline.econ.ship_out + self.pipeline.econ.packaging
        self.db.record_sale(inv, price, fees, ship)
        row = next(r for r in reports.ledger(self.db, self.pipeline.econ) if r["id"] == inv)
        self.tg.send(f"Sale logged. #{inv} profit €{row['profit']:.2f} ({row['per_hour']}).")

    def cmd_stats(self, arg: str) -> None:
        self.tg.send(html.escape(reports.summary_text(self.db, self.pipeline.econ)))
