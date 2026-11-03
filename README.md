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

## On-demand Kleinanzeigen search (opt-in)

Set `HUNTBOT_ONDEMAND=true` and each hunt also fetches the Kleinanzeigen results page for your query
(Munich + radius) directly: `huntbot hunt --live thinkpad defekt`, the *search Kleinanzeigen live*
checkbox in the web UI, or any Telegram query.

> ⚠️ **This breaks Kleinanzeigen's terms (§5)** and can get your IP or account restricted. It's your
> call. To keep the footprint small, it is deliberately limited:
> - 1 results page per hunt (`HUNTBOT_ONDEMAND_PAGES`, max 3), only when you trigger it, never on a schedule;
> - ≥ 30 s between requests and ≤ 100 per day, stored in the DB so restarts don't reset it;
> - an honest `huntbot/0.1` User-Agent, no login, no proxies, no CAPTCHA solving;
> - on HTTP 403/429, a CAPTCHA or the "IP eingeschränkt" page it stops for 24 h.
>
> Run it from your home connection. Alerts and the userscript keep working if it gets blocked.

The parser reads each ad's embedded JSON-LD (title, description, image) and finds the price,
location and "Versand möglich" by text pattern. CSS class changes therefore don't break it. It was
checked against the live result list on 2026-11-03: 25/25 ads parsed with price, location and
description. Kleinanzeigen fills thin local results with ads from elsewhere in Germany. Ads outside
your pickup postcodes (`HUNTBOT_HOME_PLZ_PREFIXES`, default 80–86) are scored with shipping, or not
alerted if the seller doesn't ship.

Docks, bags, chargers and other accessories are rejected by title (`not_the_device`).

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
pytest                               # 54 unit/integration tests, no network
node tests/e2e/userscript_e2e.js     # Playwright: real server + userscript on fixture pages + web UI
```

The e2e test answers all kleinanzeigen.de URLs from `tests/fixtures/` through `page.route()`, so it
never contacts the site.

## Things to verify on first real use

**Verified on 2026-11-03 against the live site:** search URL format
(`/s-muenchen/preis::150/<query>/k0l6411r30`, location and price filter applied) and result-list
parsing (server and userscript use the same layout-independent approach).

**Not yet checked against live pages:**

- **Alert email layout.** Save one real alert as `.eml` and run `huntbot parse-email alert.eml`.
  The parser finds ads through their `/s-anzeige/<slug>/<id>` links, including tracking redirects,
  so it should survive layout changes. Price and location extraction may need adjusting.
- **Single-ad page** in the userscript (`#viewad-*` ids, falling back to `h1` and `og:` meta tags).
  If no badge appears on an ad page, run `__huntbot.extractAdPage(document, location.href)` in the console.

## Catalog coverage

On real Munich searches most ThinkPads were models not yet in `catalog.csv` (E480/E490, L490,
X1 Carbon, X1 Yoga, T460s/T470…). They are listed but get no EV until you add a row with a real
eBay sold median.
