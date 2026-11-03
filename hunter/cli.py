"""Command line: python -m hunter <command> ...   (python -m hunter -h for help)"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from hunter import config, kleinanzeigen as ka
from hunter.pipeline import GENERIC_QUERY, Hit, Hunter
from hunter.prices import PriceTable
from hunter.store import Store

KA_WARNING = """\
Kleinanzeigen live search is OFF. Your plan (§2 #1, §4.1) chose not to automate Kleinanzeigen
because its terms (§5) forbid crawlers and it IP-blocks them. To use it anyway, at your own risk,
set HUNTER_KA_LIVE=1. It then only runs when you search, one page per 30 s, max 100 pages a day,
and pauses for 24 h on any block. Meanwhile, eBay and your saved-search alert emails still work.
"""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _ka_client(s: config.Settings, store: Store):
    return ka.KleinanzeigenClient(
        store, min_interval_s=s.ka_min_interval_s, daily_cap=s.ka_daily_cap,
        block_hours=s.ka_block_hours, chromium_path=s.chromium_path, headless=s.headless, log=_log,
    )


def _fmt_money(v) -> str:
    return "—" if v is None else f"€{v:.0f}"


def print_hits(hits: list[Hit], limit: int, show_rejected: bool = False) -> None:
    shown = [h for h in hits if show_rejected or not h.score.rejected][:limit]
    if not shown:
        print("No matching deals.")
        return
    for i, h in enumerate(shown, 1):
        l, a, sc = h.listing, h.assessment, h.score
        mark = "🔥 DEAL" if sc.alert else ("⛔ " + sc.rejected if sc.rejected else "")
        dist = f" · {l.distance_km:.0f} km" if l.distance_km is not None else ""
        print(f"\n{i:>2}. {l.title[:90]}  {mark}")
        print(f"    {l.price_text or _fmt_money(l.price_eur)} · {l.location or l.source}{dist}"
              f" · {'pickup' if sc.pickup else 'shipped'} · {l.source}")
        if sc.ev is not None:
            print(f"    EV {_fmt_money(sc.ev)} · {_fmt_money(sc.ev_per_hour)}/h · "
                  f"ROI {100 * (sc.roi or 0):.0f}% · max buy {_fmt_money(sc.max_buy)} · {sc.hours:.1f} h")
        elif sc.max_buy is not None:
            print(f"    pay at most {_fmt_money(sc.max_buy)}")
        print(f"    {a.model_guess[:70]} → {a.likely_fix[:70]} (p_ok {a.p_ok:.0%}, by {h.screened_by})")
        if a.red_flags:
            print(f"    flags: {', '.join(a.red_flags)}")
        if sc.reasons:
            print(f"    note: {'; '.join(sc.reasons)}")
        if getattr(a, "offer_message_de", None):
            print(f"    offer €{a.offer_eur:.0f}: {a.offer_message_de}")
        print(f"    {l.url}")


def _hits_json(hits: list[Hit]) -> str:
    return json.dumps([{
        "listing": h.listing.to_dict(), "assessment": h.assessment.model_dump(),
        "score": h.score.__dict__, "screened_by": h.screened_by,
    } for h in hits], ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
def cmd_search(args, s, store, prices) -> None:
    if args.postal:
        s.home_postal_code = args.postal
    if args.radius:
        s.radius_km = args.radius
    if args.no_claude:
        s.use_claude = False
    hunter = Hunter(s, store, prices, log=_log)
    plan = hunter.plan(args.query, args.max_price)
    _log(f"Hunting: {plan.summary}")
    _log(f"  Kleinanzeigen searches: {plan.kleinanzeigen_queries} · eBay: {plan.ebay_query!r}"
         + (f" · max €{plan.max_price:.0f}" if plan.max_price else ""))

    listings = []
    use_ka = not args.no_kleinanzeigen and s.ka_live_enabled
    if not args.no_kleinanzeigen and not s.ka_live_enabled:
        _log(KA_WARNING)
    with (_ka_client(s, store) if use_ka else nullcontext()) as kac:
        if kac is not None:
            for q in plan.kleinanzeigen_queries:
                _log(f"Kleinanzeigen: {q!r}")
                try:
                    batch = kac.search(q, postal_code=s.home_postal_code, radius_km=s.radius_km,
                                       max_price=plan.max_price, category=args.category or plan.category,
                                       pages=args.pages)
                except ka.Blocked as e:
                    _log(f"  stopped: {e}")
                    break
                _log(f"  {len(batch)} listings")
                listings += batch
        if not args.no_ebay:
            if s.ebay_enabled:
                from hunter.ebay import EbayClient

                _log(f"eBay (for parts / not working): {plan.ebay_query!r}")
                try:
                    batch = EbayClient(s.ebay_client_id, s.ebay_client_secret).search(
                        plan.ebay_query, max_price=plan.max_price, postal_code=s.home_postal_code)
                    _log(f"  {len(batch)} listings")
                    listings += batch
                except Exception as e:  # noqa: BLE001
                    _log(f"  eBay failed: {e}")
            else:
                _log("eBay skipped (set EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).")

        # de-duplicate across the query variants
        uniq = list({l.key: l for l in listings}.values())
        if not uniq:
            print("Nothing found. Check the sources above.")
            return
        hits = hunter.evaluate(uniq, args.query, plan=plan, ka_client=kac,
                               details=args.details if kac is not None else 0, deep=args.deep)

    if args.json:
        print(_hits_json(hits[: args.limit]))
    else:
        print_hits(hits, args.limit, args.show_rejected)
        deals = sum(1 for h in hits if h.score.alert)
        print(f"\n{len(hits)} listings screened, {deals} pass your buy rules "
              f"(EV ≥ €{s.min_ev:.0f}, ≥ €{s.min_ev_per_hour:.0f}/h, ROI ≥ {s.min_roi:.0%}).")
    if args.notify:
        _log(f"Sent {hunter.notify(hits)} Telegram card(s).")


def cmd_score(args, s, store, prices) -> None:
    if args.no_claude:
        s.use_claude = False
    hunter = Hunter(s, store, prices, log=_log)
    if args.html:
        listing = ka.parse_ad(Path(args.html).read_text(encoding="utf-8"), args.url)
        kac = None
        ctx = nullcontext()
    else:
        if not s.ka_live_enabled:
            sys.exit("Reading an ad URL needs HUNTER_KA_LIVE=1, or save the page and pass --html FILE.\n\n" + KA_WARNING)
        ctx = _ka_client(s, store)
    with ctx as kac:
        if not args.html:
            listing = kac.ad(args.url)
        if args.distance is not None:
            listing.distance_km = args.distance
        hits = hunter.evaluate([listing], args.query or GENERIC_QUERY, deep=1)
    print_hits(hits, 1, show_rejected=True)


def _watch_once(args, s, store, prices, hunter: Hunter) -> None:
    listings = []
    if s.imap_enabled:
        from hunter.email_alerts import fetch_alerts

        try:
            batch = fetch_alerts(s.imap_host, s.imap_user, s.imap_password, s.imap_folder, days=args.days)
            new = [l for l in batch if store.get_listing(l.key) is None]
            _log(f"alert emails: {len(batch)} ads, {len(new)} new")
            listings += new
        except Exception as e:  # noqa: BLE001
            _log(f"IMAP failed: {e}")
    if s.ebay_enabled and args.query:
        from hunter.ebay import EbayClient

        eb = EbayClient(s.ebay_client_id, s.ebay_client_secret)
        for q in args.query:
            try:
                batch = eb.search(q, max_price=args.max_price, postal_code=s.home_postal_code)
                new = [l for l in batch if store.get_listing(l.key) is None]
                _log(f"eBay {q!r}: {len(batch)} items, {len(new)} new")
                listings += new
            except Exception as e:  # noqa: BLE001
                _log(f"eBay {q!r} failed: {e}")
    if not listings:
        return
    use_ka = s.ka_live_enabled and args.details > 0
    with (_ka_client(s, store) if use_ka else nullcontext()) as kac:
        hits = hunter.evaluate(listings, args.goal, ka_client=kac, details=args.details, deep=args.deep)
    if args.dry_run:
        print_hits(hits, 20)
    else:
        sent = hunter.notify(hits)
        _log(f"{sum(h.score.alert for h in hits)} deals, {sent} Telegram card(s) sent")


def cmd_watch(args, s, store, prices) -> None:
    if not (s.imap_enabled or (s.ebay_enabled and args.query)):
        sys.exit("Nothing to watch: set HUNTER_IMAP_* (alert emails) and/or EBAY_* plus --query.")
    if not s.telegram_enabled and not args.dry_run:
        _log("Telegram isn't configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); deals will only be stored.")
    hunter = Hunter(s, store, prices, log=_log)
    while True:
        _log(time.strftime("[%H:%M] checking…"))
        store.purge_old()
        _watch_once(args, s, store, prices, hunter)
        if args.once:
            return
        time.sleep(args.every * 60)


def cmd_bought(args, s, store, prices) -> None:
    listing = store.get_listing(args.ref) if ":" in args.ref else None
    title = listing.title if listing else args.ref
    item = store.add_purchase(title, args.price, key=listing.key if listing else None,
                              serial=args.serial, notes=args.notes or "")
    print(f"Logged purchase #{item}: {title} for €{args.price:.2f}")


def cmd_cost(args, s, store, prices) -> None:
    store.add_cost(args.id, parts=args.parts, other=args.other, hours=args.hours)
    print(f"Updated #{args.id}.")


def cmd_sold(args, s, store, prices) -> None:
    fees = args.fees if args.fees is not None else round(args.price * s.sale_fee_pct, 2)
    store.mark_sold(args.id, args.price, fees)
    print(f"Marked #{args.id} sold for €{args.price:.2f} (fees €{fees:.2f}).")


def cmd_inventory(args, s, store, prices) -> None:
    rows = store.inventory()
    if not rows:
        print("No purchases logged yet.")
        return
    total, hours = 0.0, 0.0
    for r in rows:
        line = f"#{r['id']:<3} {r['title'][:50]:<50} buy €{r['buy_price']:.0f} parts €{r['parts_cost']:.0f}"
        if r["sold_price"] is not None:
            vat = max(0.0, r["sold_price"] - r["buy_price"]) * 19 / 119 if s.vat_mode == "25a" else 0.0
            profit = r["sold_price"] - r["sale_fees"] - vat - r["buy_price"] - r["parts_cost"] - r["other_cost"]
            total += profit
            hours += r["labour_hours"]
            line += f" → sold €{r['sold_price']:.0f}, profit €{profit:.0f}"
            if r["labour_hours"]:
                line += f" (€{profit / r['labour_hours']:.0f}/h)"
        else:
            line += " (in stock)"
        print(line)
    print(f"\nRealised profit €{total:.0f}" + (f" over {hours:.1f} h = €{total / hours:.0f}/h" if hours else ""))


def cmd_export(args, s, store, prices) -> None:
    store.export_csv(Path(args.path), s.vat_mode)
    print(f"Wrote {args.path}")


def cmd_prices(args, s, store, prices) -> None:
    for m in prices.models:
        print(f"{m.key:<20} {m.label:<45} resale €{m.resale_p50:>4.0f}  salvage €{m.salvage:>3.0f}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m hunter", description="Find repair-and-flip deals.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="hunt for deals matching a plain-language query")
    sp.add_argument("query", help='e.g. "thinkpad t14 defekt unter 150 euro"')
    sp.add_argument("--postal", help="your postcode (default HUNTER_POSTAL_CODE)")
    sp.add_argument("--radius", type=int, help="pickup radius km (default HUNTER_RADIUS_KM)")
    sp.add_argument("--max-price", type=float)
    sp.add_argument("--category", help="kleinanzeigen category, e.g. notebooks")
    sp.add_argument("--pages", type=int, default=1, help="result pages per Kleinanzeigen search (default 1)")
    sp.add_argument("--details", type=int, default=3, help="read the full ad page for the top N (default 3)")
    sp.add_argument("--deep", type=int, default=3, help="deep-review the top N with the stronger model")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--no-kleinanzeigen", action="store_true")
    sp.add_argument("--no-ebay", action="store_true")
    sp.add_argument("--no-claude", action="store_true", help="keyword rules only (free)")
    sp.add_argument("--show-rejected", action="store_true")
    sp.add_argument("--notify", action="store_true", help="send deals to Telegram")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("score", help="score one Kleinanzeigen ad (URL, or a saved page via --html)")
    sp.add_argument("url")
    sp.add_argument("--html", help="saved ad page (no request to Kleinanzeigen)")
    sp.add_argument("--query", help="what you're hunting for (default: business laptops / Dyson)")
    sp.add_argument("--distance", type=float, help="km from you (for travel cost)")
    sp.add_argument("--no-claude", action="store_true")
    sp.set_defaults(fn=cmd_score)

    sp = sub.add_parser("watch", help="poll alert emails + eBay and push deals to Telegram")
    sp.add_argument("--query", action="append", default=[], help="eBay search to watch (repeatable)")
    sp.add_argument("--goal", default=GENERIC_QUERY, help="what you're hunting for, for the screener")
    sp.add_argument("--max-price", type=float)
    sp.add_argument("--every", type=float, default=15, help="minutes between checks (default 15)")
    sp.add_argument("--days", type=int, default=2, help="look at alert emails from the last N days")
    sp.add_argument("--details", type=int, default=0, help="with HUNTER_KA_LIVE=1: read N ad pages per run")
    sp.add_argument("--deep", type=int, default=3)
    sp.add_argument("--once", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("bought", help="log a purchase")
    sp.add_argument("ref", help="listing key from the results (e.g. kleinanzeigen:3550598646) or a title")
    sp.add_argument("price", type=float)
    sp.add_argument("--serial")
    sp.add_argument("--notes")
    sp.set_defaults(fn=cmd_bought)

    sp = sub.add_parser("cost", help="add parts / other costs / hours to an item")
    sp.add_argument("id", type=int)
    sp.add_argument("--parts", type=float, default=0.0)
    sp.add_argument("--other", type=float, default=0.0)
    sp.add_argument("--hours", type=float, default=0.0)
    sp.set_defaults(fn=cmd_cost)

    sp = sub.add_parser("sold", help="mark an item sold")
    sp.add_argument("id", type=int)
    sp.add_argument("price", type=float)
    sp.add_argument("--fees", type=float, help="platform fees (default HUNTER_SALE_FEE_PCT of price)")
    sp.set_defaults(fn=cmd_sold)

    sub.add_parser("inventory", help="show purchases and realised profit").set_defaults(fn=cmd_inventory)
    sp = sub.add_parser("export", help="CSV with the per-item §25a record for your tax advisor")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_export)
    sub.add_parser("prices", help="show the comparison-price table").set_defaults(fn=cmd_prices)
    sub.add_parser("purge", help="delete unbought listings older than 60 days").set_defaults(
        fn=lambda a, s, st, pr: print(f"Purged {st.purge_old()} listings."))

    args = p.parse_args(argv)
    s = config.load()
    store = Store(s.db_path)
    prices = PriceTable.load()
    args.fn(args, s, store, prices)
