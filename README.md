# Huntbot: deal finder for repair-and-flip

Type a query such as `thinkpad t14 defekt bis 150`. Huntbot collects matching listings from
**eBay**, **your Kleinanzeigen alert emails** and **the Kleinanzeigen pages you browse**. It ranks
them by **expected profit** after fees, §25a margin VAT, the warranty reserve and the risk that the
repair fails. It also flags locked or stolen-looking devices and drafts an offer for you to send.

This implements the "Tier 0" design from the consolidated plan (§4). The bot never sends automated
requests to kleinanzeigen.de and never messages sellers.

```
 Kleinanzeigen alert emails ─┐ (IMAP, your own mailbox)
 eBay Browse API ────────────┼─► dedup ─► rules + Claude Haiku 5.5 ─► Sonnet 5.5 if promising ─► EV score
 Userscript (pages you open) ┘                                                                    │
                                     web UI · Telegram cards · EV badges on Kleinanzeigen pages ◄─┘
                                     inventory / profit / §25a / DAC7 ledger
```

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # fill in whatever you have; every source is optional
huntbot serve                 # http://127.0.0.1:8787
```

Then:

1. **Userscript.** Install [Tampermonkey](https://www.tampermonkey.net/) or Violentmonkey and add
   `userscript/huntbot.user.js`. In the Tampermonkey menu choose *Huntbot: set API token* and enter
   your `HUNTBOT_TOKEN`. Then browse Kleinanzeigen normally. Every result list and ad page you open
   gets EV badges, and the listings are added to your local search index.
2. **Alert emails.** Create 10–15 saved searches on Kleinanzeigen (e.g. "thinkpad defekt",
   "latitude bastler", "laptop passwort vergessen"). Send the alerts to a dedicated Gmail account and
   put an [app password](https://myaccount.google.com/apppasswords) in `IMAP_PASSWORD`. `serve` reads
   new alerts every 2 minutes.
3. **eBay.** Get production keys at developer.ebay.com and set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.
   Hunts then also query live "for parts / not working" listings in Germany.
4. **Claude.** Set `ANTHROPIC_API_KEY`. Without it, the bot scores with German keyword rules only.
5. **Telegram.** Create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   New listings that pass the buy rule arrive as cards with *Open ad*, *Copy offer*, *Pass* and *Bought* buttons.

## Searching

| Where | How |
|---|---|
| Web UI | `http://127.0.0.1:8787`, type a query |
| Terminal | `huntbot hunt thinkpad t14 bis 150` |
| Telegram | send the query as a plain message, or `/hunt …` |

Query syntax: free text plus `bis 150` / `max 150` / `unter 150`, `ab 50`, `50-150 €`, and
`versand` (shipping only). Each hunt:

- searches eBay live with up to 3 query variants (defekt / bastler / …);
- full-text searches everything already collected from alert emails and the userscript;
- scores everything with rules, then sends the top 15 to Claude (`--budget` changes this);
- returns ready-made **Kleinanzeigen search links** (Munich + radius, and Germany-wide). Open them
  with the userscript to score that page, or save them as alerts.

## How a listing is scored

```
net_sale = resale_p50 − 5% eBay fee − shipping − packaging − §25a VAT (19/119 of margin) − 4% warranty reserve
cost_in  = buy (asking × 0.85 if VB) + Sicher-bezahlen fee + parts + travel (km × €0.30) or inbound shipping
EV       = p_ok × net_sale + (1 − p_ok) × salvage − cost_in        (p_ok capped at 0.9)
alert if EV ≥ €40 AND EV/h ≥ €25 AND EV/(buy + parts) ≥ 40%, or the item is free
```

- **Rejected:** activation/iCloud/FRP lock, MDM/Autopilot, or "not the device" (box or part only).
- **Discounted:** BIOS password ×0.5, company asset tag ×0.5, stock photos ×0.7. Missing charger adds €25.
- **Max buy** is the highest price that still clears the €40 EV rule. The drafted offer anchors at
  85% of it and always asks about the BIOS password, account sign-out and the serial number.
- The test suite checks the formula against the plan's §6 worked example (T14 G2: €70.20 profit).

Thresholds are set in `.env` (`HUNTBOT_MIN_EV`, `HUNTBOT_MIN_EV_PER_HOUR`, `HUNTBOT_MIN_ROI`,
`HUNTBOT_KLEINUNTERNEHMER`).

### ⚠️ Replace the resale prices

`huntbot/data/catalog.csv` holds **estimated** resale medians (`label=EST`) for 19 models. Before
trusting any EV, replace them with real eBay *sold* medians, 10+ results per model (each hunt links
to the sold-items search). Then change `label` to `SOLD`. To add a model, add a row: rows match top to
bottom, so put specific patterns first.

## Tracking flips

Telegram: `/bought <listing-key or title> <€>`, `/cost <id> <€> [note]`, `/time <id> <minutes>`,
`/sold <id> <€> [fees] [shipping]`, `/stats`.

- `huntbot stats` shows profit, €/h, DAC7 counters (30 sales / €2,000) and the day-90 gate.
- `huntbot export --out ledger.csv` writes a per-item ledger (buy, costs, sale, §25a margin and VAT)
  for your tax advisor.

## Privacy (GDPR)

- Phone numbers, email addresses and street addresses are removed before storage and before any LLM call.
- Seller names are stored only as salted hashes, and location is reduced to postcode and district.
- `huntbot purge --days 60` deletes listings you never bought. Purchase records are kept (§147 AO).
  Run it from cron.

## Testing

```bash
pytest                               # 37 unit/integration tests, no network
node tests/e2e/userscript_e2e.js     # Playwright: real server + userscript on fixture pages + web UI
```

The e2e test answers all kleinanzeigen.de URLs from `tests/fixtures/` through `page.route()`, so it
never contacts the site.

## Things to verify on first real use

The selectors and URL formats below are written from knowledge of the site's markup and have **not
been checked against live pages**:

- **Alert email layout.** Save one real alert as `.eml` and run `huntbot parse-email alert.eml`.
  The parser finds ads through their `/s-anzeige/<slug>/<id>` links, including tracking redirects,
  so it should survive layout changes. Price and location extraction may need adjusting.
- **Userscript selectors** (`article.aditem`, `#viewad-title`, …). Each has a generic fallback. If
  badges don't appear, open the browser console and run `__huntbot.extractSearchPage(document)`.
- **Search URL format** `/s-muenchen/preis::150/<query>/k0l6411r30`. Open one generated link and
  check that the location and price filters applied.

## Why not a Playwright scraper for Kleinanzeigen?

Kleinanzeigen's terms (§5) ban automated access, including read-only, and the site IP-blocks
scrapers. The plan's research flagged an account or IP ban as risk #3, and data-scraping SaaS as a
legal risk. Your own browser plus the userscript gives the same result (type a query, see ranked
deals on the page) with no automated traffic. Playwright is used here to test that flow end to end.
