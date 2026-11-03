"""Command line entry point.

  huntbot hunt "thinkpad t14 defekt bis 150"   ranked results in the terminal
  huntbot serve                                web UI + userscript endpoint + email poller + Telegram bot
  huntbot poll-email [--once]                  read Kleinanzeigen alert mails
  huntbot parse-email alert.eml                debug the email parser on a saved mail
  huntbot stats | export | purge
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from . import reports
from .config import load_settings
from .db import DB
from .pipeline import Pipeline

log = logging.getLogger("huntbot")


def _build(with_notifier: bool = True):
    settings = load_settings()
    db = DB(settings.db_path)
    tg = None
    if with_notifier and settings.telegram_enabled:
        from .telegram import Telegram
        tg = Telegram(settings.telegram_token, settings.telegram_chat_id)
    pipeline = Pipeline(settings, db, notifier=tg.send_card if tg else None)
    ebay = None
    if settings.ebay_enabled:
        from .sources.ebay import EbayClient
        ebay = EbayClient(settings.ebay_client_id, settings.ebay_client_secret, settings.ebay_zip)
    return settings, db, pipeline, ebay, tg


def _live(settings, db, force: bool = False):
    if not (settings.ondemand_enabled or force):
        return None
    from .sources.kleinanzeigen_live import LiveSearch
    return LiveSearch(db)


def cmd_hunt(args) -> None:
    settings, db, pipeline, ebay, _ = _build(with_notifier=False)
    live = _live(settings, db, force=args.live)
    out = pipeline.hunt(" ".join(args.query), ebay=ebay, llm_budget=args.budget, live=live)
    for i, info in enumerate(out.get("live", [])):
        print(f"Kleinanzeigen live: {info['count']} listings from {info['url']}")
        if args.save_html:
            Path(f"{args.save_html}.{i}.html").write_text(info["html"], encoding="utf-8")
    results = out["results"][: args.limit]
    if not results:
        print("No stored or eBay listings matched. Open the Kleinanzeigen links below with the userscript installed.")
    for r in results:
        s = r.score
        mark = "✗" if s.rejected else ("★" if s.alert else " ")
        price = "free" if r.listing.price == 0 else (f"€{r.listing.price:.0f}" if r.listing.price is not None else "?")
        print(f"{mark} EV €{s.ev:6.0f}  €{s.ev_per_hour:4.0f}/h  max €{s.max_buy:4.0f}  {price:>6}  "
              f"[{r.listing.source[:2]}] {r.listing.title[:60]}")
        print(f"     {r.screen.device_model} · {r.screen.fault_summary[:70]} · flags: {', '.join(r.screen.red_flags) or '-'}")
        print(f"     {r.listing.url}")
    print("\nKleinanzeigen searches:")
    for link in out["kleinanzeigen_links"]:
        print(f"  {link['phrase']:<40} {link['url']}")
    print(f"eBay sold prices: {out['ebay_sold_url']}")
    for err in out["errors"]:
        print(f"! {err}", file=sys.stderr)


def poll_email_once(settings, pipeline) -> int:
    from .sources.kleinanzeigen import fetch_alert_emails
    listings = fetch_alert_emails(settings.imap_host, settings.imap_user, settings.imap_password, settings.imap_folder)
    for listing in listings:
        pipeline.process(listing, use_llm=True)
    return len(listings)


def email_loop(settings, pipeline, interval: int) -> None:
    while True:
        try:
            n = poll_email_once(settings, pipeline)
            if n:
                log.info("processed %d listings from alert mails", n)
        except Exception:
            log.exception("email poll failed")
        time.sleep(interval)


def cmd_poll_email(args) -> None:
    settings, db, pipeline, *_ = _build()
    if not settings.imap_enabled:
        sys.exit("Set IMAP_USER and IMAP_PASSWORD in .env first.")
    if args.once:
        print(f"{poll_email_once(settings, pipeline)} listings processed")
    else:
        email_loop(settings, pipeline, args.interval)


def cmd_parse_email(args) -> None:
    from .sources.kleinanzeigen import parse_alert_email
    for l in parse_alert_email(Path(args.file).read_bytes()):
        print(f"{l.ad_id}  {l.price}{' VB' if l.negotiable else ''}  {l.location:<20} {l.title}\n    {l.url}")


def cmd_serve(args) -> None:
    import uvicorn
    from .web import create_app
    settings, db, pipeline, ebay, tg = _build()
    if settings.imap_enabled and not args.no_email:
        threading.Thread(target=email_loop, args=(settings, pipeline, args.email_interval), daemon=True).start()
        log.info("polling alert mails every %ss", args.email_interval)
    if tg and not args.no_telegram:
        from .telegram import Bot
        threading.Thread(target=Bot(tg, pipeline, db, ebay, live=_live(settings, db)).run_forever, daemon=True).start()
    if not settings.token:
        log.warning("HUNTBOT_TOKEN is empty: the API accepts requests from any local program")
    uvicorn.run(create_app(settings, db, pipeline, ebay, live=_live(settings, db)), host=args.host, port=args.port or settings.port)


def cmd_price(args) -> None:
    """Estimate prices for a device description on both platforms."""
    from .features import extract
    from .pricemodel import can_price, fit_from_db
    settings = load_settings()
    model = fit_from_db(DB(settings.db_path))
    title = " ".join(args.title)
    f = extract(title, args.desc or "")
    print(f"features: {f.as_dict()}")
    if not can_price(f):
        sys.exit("Can't price this: no known product line / generation in the text.")
    from dataclasses import replace
    rows = [("Kleinanzeigen typical ask", "kleinanzeigen", "ask"), ("Kleinanzeigen sold (local)", "kleinanzeigen", "sold"),
            ("eBay typical ask", "ebay", "ask"), ("eBay sold", "ebay", "sold")]
    for label, platform, kind in rows:
        e = model.predict(replace(f, platform=platform, price_kind=kind))
        print(f"  {label:<28} €{e.price:5.0f}   (80%: €{e.low:.0f}–€{e.high:.0f})")
    new = model.predict(replace(f, condition="new", platform="ebay", price_kind="ask", year=features_year_now()))
    print(f"  {'new-price proxy (age 0)':<28} €{new.price:5.0f}   rough; check Geizhals/idealo for real retail")
    if args.ask:
        fair = model.predict(f)
        gap = 1 - args.ask / fair.price
        print(f"\n  asking €{args.ask:.0f} vs typical €{fair.price:.0f}: {abs(gap):.0%} {'under' if gap > 0 else 'over'} market")
    print(f"\n  source: {model.predict(f).source}")


def features_year_now() -> int:
    from .features import THIS_YEAR
    return THIS_YEAR


def cmd_comp(args) -> None:
    """Record real prices (eBay sold, your local sales) – they train the model 3× as strongly as asks."""
    settings = load_settings()
    db = DB(settings.db_path)
    if args.comp_cmd == "add":
        cid = db.add_comp(" ".join(args.title), args.price, args.platform, args.kind, condition=args.condition)
        print(f"comp #{cid} saved")
    elif args.comp_cmd == "import":
        import csv
        n = 0
        with open(args.file, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                db.add_comp(row["title"], float(row["price"].replace(",", ".")), row.get("platform") or "ebay",
                            row.get("kind") or "sold", row.get("description", ""), row.get("condition", ""))
                n += 1
        print(f"imported {n} comps")


def cmd_model(args) -> None:
    from .pricemodel import PRIOR, fit_from_db
    settings = load_settings()
    model = fit_from_db(DB(settings.db_path))
    print(f"trained on {model.n} rows, residual sd {model.sigma:.2f} (log), lines: {model.line_counts}")
    for name, value in model.explain().items():
        print(f"  {name:<22} {value:+.3f}   (prior {PRIOR[name]:+.3f})")
    if args.export:
        Path(args.export).write_text(model.to_json(), encoding="utf-8")
        print(f"wrote {args.export}")


def cmd_run_once(args) -> None:
    """One unattended pass for cron / GitHub Actions: alert mails + saved eBay hunts → Telegram."""
    settings, db, pipeline, ebay, tg = _build()
    total = 0
    if settings.imap_enabled:
        try:
            total += poll_email_once(settings, pipeline)
        except Exception as exc:
            log.error("email poll failed: %s", exc)
    hunts = [h.strip() for h in Path(args.hunts).read_text(encoding="utf-8").splitlines()
             if h.strip() and not h.strip().startswith("#")] if Path(args.hunts).exists() else []
    if ebay is not None:
        for query in hunts:
            try:
                out = pipeline.hunt(query, ebay=ebay, llm_budget=args.budget, notify=True)
                total += len(out["results"])
            except Exception as exc:
                log.error("hunt %r failed: %s", query, exc)
    if tg and args.telegram_commands:
        from .telegram import Bot
        n = Bot(tg, pipeline, db, ebay).process_pending()
        log.info("answered %d queued Telegram updates", n)
    alerts = db.conn.execute("SELECT COUNT(*) AS n FROM listings WHERE status='notified'").fetchone()["n"]
    print(f"processed {total} listings; {alerts} alerts sent so far; telegram={'on' if tg else 'off'}")


def cmd_stats(args) -> None:
    settings = load_settings()
    print(reports.summary_text(DB(settings.db_path), settings.economics))


def cmd_export(args) -> None:
    settings = load_settings()
    Path(args.out).write_text(reports.ledger_csv(DB(settings.db_path), settings.economics), encoding="utf-8")
    print(f"wrote {args.out}")


def cmd_purge(args) -> None:
    settings = load_settings()
    print(f"deleted {DB(settings.db_path).purge(args.days)} listings older than {args.days} days")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="huntbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hunt", help="search and rank")
    h.add_argument("query", nargs="+")
    h.add_argument("--budget", type=int, default=15, help="max listings sent to Claude")
    h.add_argument("--limit", type=int, default=20)
    h.add_argument("--save-html", metavar="PREFIX", help="debug: save fetched Kleinanzeigen pages")
    h.add_argument("--live", action="store_true",
                   help="also fetch one Kleinanzeigen results page now (opt-in, breaks ToS §5, rate-limited)")
    h.set_defaults(func=cmd_hunt)

    s = sub.add_parser("serve", help="run web UI, userscript endpoint, mail poller and Telegram bot")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--email-interval", type=int, default=120)
    s.add_argument("--no-email", action="store_true")
    s.add_argument("--no-telegram", action="store_true")
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser("poll-email", help="process Kleinanzeigen alert emails")
    e.add_argument("--once", action="store_true")
    e.add_argument("--interval", type=int, default=120)
    e.set_defaults(func=cmd_poll_email)

    pe = sub.add_parser("parse-email", help="debug: parse a saved .eml alert")
    pe.add_argument("file")
    pe.set_defaults(func=cmd_parse_email)

    pr = sub.add_parser("price", help="estimate what a device sells for on Kleinanzeigen and eBay")
    pr.add_argument("title", nargs="+")
    pr.add_argument("--desc", default="")
    pr.add_argument("--ask", type=float, help="asking price to compare")
    pr.set_defaults(func=cmd_price)

    cp = sub.add_parser("comp", help="record real sold prices to train the price model")
    cps = cp.add_subparsers(dest="comp_cmd", required=True)
    ca = cps.add_parser("add")
    ca.add_argument("price", type=float)
    ca.add_argument("title", nargs="+")
    ca.add_argument("--platform", choices=["ebay", "kleinanzeigen"], default="ebay")
    ca.add_argument("--kind", choices=["sold", "ask"], default="sold")
    ca.add_argument("--condition", default="")
    ci = cps.add_parser("import", help="CSV with columns title,price[,platform,kind,description,condition]")
    ci.add_argument("file")
    cp.set_defaults(func=cmd_comp)

    md = sub.add_parser("model", help="show (and export) the trained price model")
    md.add_argument("--export", help="write coefficients JSON (used by the Claude skill)")
    md.set_defaults(func=cmd_model)

    ro = sub.add_parser("run-once", help="one unattended pass (cron / GitHub Actions)")
    ro.add_argument("--hunts", default="hunts.txt", help="saved eBay queries, one per line")
    ro.add_argument("--budget", type=int, default=5)
    ro.add_argument("--telegram-commands", action="store_true", help="also answer queued Telegram commands")
    ro.set_defaults(func=cmd_run_once)

    sub.add_parser("mcp", help="run the MCP server on stdio (Claude Desktop / Claude Code)").set_defaults(
        func=lambda a: __import__("huntbot.mcp_server", fromlist=["main"]).main())

    sub.add_parser("stats").set_defaults(func=cmd_stats)
    ex = sub.add_parser("export", help="ledger CSV for your tax advisor")
    ex.add_argument("--out", default="ledger.csv")
    ex.set_defaults(func=cmd_export)
    pu = sub.add_parser("purge", help="GDPR retention: delete unbought listings")
    pu.add_argument("--days", type=int, default=60)
    pu.set_defaults(func=cmd_purge)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
