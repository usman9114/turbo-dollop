# Huntbot: deal finder for repair-and-flip

Type a query such as `thinkpad t14 defekt bis 150`. Huntbot collects matching listings from
**eBay**, **your Kleinanzeigen alert emails** and **the Kleinanzeigen pages you browse**. It ranks
them by **expected profit** after fees, §25a margin VAT, the warranty reserve and the risk that the
repair fails. It also flags locked or stolen-looking devices and drafts an offer for you to send.

This implements the "Tier 0" design from the consolidated plan (§4). The bot never sends automated
requests to kleinanzeigen.de and never messages sellers.

```
 Kleinanzeigen alert emails ─┐ (IMAP, your own mailbox)
 eBay Browse API ────────────┼─► dedup ─► rules ─► screening team (OpenRouter) or Claude ─► EV score
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

## Price model: what is it worth, and on which platform?

Kleinanzeigen and eBay.de are **separate markets**. Kleinanzeigen is local, cash and negotiable,
with lower prices and asking prices above what things sell for. eBay is nationwide, with buyer
protection and higher prices, but a 5% fee and ~€9 shipping. The model predicts each platform
separately.

`huntbot/pricemodel.py` is a log-linear ridge regression:

```
log(price) = b0 + b_line + b_age·age + b_ram·log2(RAM/8) + b_ssd·log2(SSD/256)
             + b_condition + b_no_charger + b_ebay + b_sold + b_vb + unknown-spec flags
```

- **Features come from the listing text** (`features.py`). The product line gives the price tier
  (X1 > T > L > E, Latitude 7/5/3, EliteBook, MacBook …). The CPU generation gives the age (i5-8350U
  → 2018, i5-1135G7 → 2021, Ryzen 5650U → 2021, M1 → 2020; HP G-numbers, Dell 54x0). RAM, SSD,
  condition (new / like new / good / worn / defect) and charger are read from the text too. Devices
  not in the catalog still get a price.
- **Priors + learning.** Coefficients start at hand-set priors calibrated to the plan's research
  (T480 ≈ €185, T14 G2 ≈ €320 on Kleinanzeigen). They are shrunk toward those priors, so day one
  is sensible, and they move as data comes in. Every listing seen trains the model (weight 1).
  **Sold prices you record train it 3× as strongly**, and so do your own sales.
- Each estimate has an **80% range**, wider for rarely seen lines or unknown generation.

```bash
huntbot price ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB --desc "funktioniert" --ask 180
huntbot comp add 265 ThinkPad T14 Gen 2 i5-1135G7 16GB --platform ebay      # a real sold price
huntbot comp import sold.csv          # title,price[,platform,kind,description,condition]
huntbot model                         # coefficients vs priors; --export model.json for the skill
```

The "new-price proxy" in `huntbot price` is a rough age-0 extrapolation. For real retail prices,
check Geizhals or idealo (the Claude skill does this).

## Quick flips (no repair)

A working device listed well under market is the fastest money: clean it, reset Windows, photograph
and list it, about 45 minutes. When the listing shows no fault, the scorer switches to **flip mode**:

- resale = predicted *sold* price on eBay **and** on Kleinanzeigen; whichever nets more after fees wins;
- 0.75 h and €3 of cleaning supplies, p_ok 0.9 (risk it isn't as described);
- **buy signal** when profit ≥ €30 (`HUNTBOT_FLIP_MIN_EV`), ≥ €25/h, **and** the ad is ≥ 25%
  under the typical price for that device on that platform (`HUNTBOT_FLIP_MIN_DISCOUNT`).

Telegram cards and the web UI show `⚡ QUICK FLIP · 48% under typical €363`, where to sell, and
the price range.

Guards learned from the live test: spare parts that name a model (keyboard, display panel, privacy
filter) are rejected. A "working" device more than 70% under typical gets no buy signal and a
"part only, scam or stolen?" check. A stated *Kaufpreis* or *Neupreis* caps the estimate (an unused
T14 Gen 4 "bought Feb 2026 for €799" isn't worth €910). Local ads are costed as pickup even if
the seller ships.

On the 66 real Munich ThinkPad ads tested on 2026-11-03, nothing qualified: all working devices
were listed near typical price. Real flips are rare and fast, which is why push alerts matter.

## How a listing is scored

```
net_sale = resale_p50 − 5% eBay fee − shipping − packaging − §25a VAT (19/119 of margin) − 4% warranty reserve
cost_in  = buy (asking × 0.85 if VB) + Sicher-bezahlen fee + parts + travel (km × €0.30) or inbound shipping
EV       = p_ok × net_sale + (1 − p_ok) × salvage − cost_in        (p_ok capped at 0.9)
alert if EV ≥ €40 AND EV/h ≥ €25 AND EV/(buy + parts) ≥ 40%, or the item is free
```

- **Rejected:** activation/iCloud/FRP lock, MDM/Autopilot, "not the device" (box or part only), scam wording (PayPal Friends, prepayment only, "I am abroad") and wanted ads ("Suche …").
- **Discounted:** BIOS password ×0.5, company asset tag ×0.5, stock photos ×0.7, dealer listing ("ab X €", several configurations) ×0.7. Missing charger adds €25.
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

## Screening team (OpenRouter)

With `OPENROUTER_API_KEY` set, listings that could plausibly be a deal go through a small team
(plan: `docs/agent-team-plan.md`). The LLMs only **read and look**; every number (EV, max buy, p_ok,
parts, hours) and the final decision come from code.

```
rules + price model ─► gate ─► Reader ─► Inspector ─► Skeptic (only on a would-be BUY) ─► Judge (code) ─► card / web
                        │         text      photos       devil's advocate                 BUY/WATCH/PASS
                        └ not a candidate (rejected, far away, no shot at a deal) = 0 requests
```

| Role | Kind | Does |
|---|---|---|
| Reader | LLM (text) | spec, working status, fault, seller type, lock/scam signals, purchase price, German seller questions, each with a **verbatim quote** |
| Inspector | LLM (vision) | stock photo vs real, lock screens, damage, asset stickers |
| Skeptic | LLM (text) | devil's advocate, runs only when the code Judge would say BUY |
| Gatekeeper, Merger, Judge | code | gate, quote verification, merge into `Screen`/`Features`, fixed decision table |

A fact counts only if its quote really appears in the ad text (code checks it). Unverified signals can
push a listing toward WATCH, never to BUY or PASS. **BUY** needs every planned agent to succeed and
nothing to be found; a thin listing (< 60 characters, typical for alert mails) is never BUY. Opening the
ad with the userscript delivers the full text and re-screens it.

Decisions: **BUY** (alert), **WATCH** (maybe; pushed if the rules alert and `HUNTBOT_PUSH_WATCH=true`),
**PASS**, **UNSCREENED** (budget or models unavailable: rules alert, labelled), **PENDING** (queued, the
next `run-once` drains it; after 45 min it becomes UNSCREENED).

**Models / profiles** (`HUNTBOT_AGENT_PROFILE`, chains are OpenRouter fallbacks, override with
`HUNTBOT_TEAM_{READER,INSPECTOR,SKEPTIC}_MODELS`):

| Role | `glm` (default) | `free` |
|---|---|---|
| Reader | `z-ai/glm-5.3-flash` → `glm-4.7-flash` → `gemma-4-31b-it:free` | `gemma-4-31b-it:free` → `nemotron-3.5-super-vl:free` → `xing4.0-29b-a4b:free` |
| Inspector | `z-ai/glm-5.3-flash` → `nemotron-3.5-super-vl:free` → `gemma-4-31b-it:free` | `nemotron-3.5-super-vl:free` → `gemma-4-31b-it:free` → `gemma-4-26b-a4b-it:free` |
| Skeptic | `z-ai/glm-5.3` → `nemotron-3-ultra-550b-a55b:free` → `xing4.0-29b-a4b:free` | `nemotron-3-ultra-550b-a55b:free` → `xing4.0-29b-a4b:free` → `gemma-4-31b-it:free` |

**Budget.** Every request is logged in SQLite (`llm_calls`, survives Actions runs via the cache) and
reconciled with `GET /api/v1/key`. Free `:free` models: 50 requests/day (1000 after buying ≥ $10 credits
once), 20/min; the bot stays 3 under and keeps a reserve for interactive use. Paid models (GLM): capped by
`HUNTBOT_AGENT_MAX_USD_DAY` (default `0.50`) and `HUNTBOT_AGENT_MAX_CALLS_RUN` (default 40 per run);
the cost comes from `usage.cost` (fallback: tokens × live price). When a cap is hit the paid models are
dropped from the chain (free fallbacks still work), then the pipeline falls back to rules (UNSCREENED /
PENDING). Non-`:free` ids are refused unless the profile is `glm` or `HUNTBOT_OR_ALLOW_PAID=true`;
`stealth/*`, `:batch` and `~…-latest` ids are never used. OpenRouter 404 "data policy": allow free
endpoints that may log prompts under OpenRouter → Settings → Privacy. Only PII-stripped public ad text and
public image URLs are sent.

**Shadow mode** (`HUNTBOT_TEAM_ENFORCE=false`, the default): verdicts are computed, stored and shown on
Telegram cards, `huntbot hunt` and the web UI, but alerts behave exactly as before. Run `huntbot agents-eval`
and review the verdicts for a few days, then set `HUNTBOT_TEAM_ENFORCE=true`.

**Cost.** A listing that uses all three agents costs well under $0.01 on the `glm` profile; typically
only 5 to 25% of new listings reach the Reader. Check `huntbot agents-status`.

```bash
export OPENROUTER_API_KEY=sk-or-...
huntbot agents-status                                  # provider, budget, chains, recent errors
python evals/build_eval_set.py --pages <saved result pages> --db real.db   # writes evals/data/ads.jsonl (git-ignored)
huntbot agents-eval --role reader --models z-ai/glm-5.3-flash,google/gemma-4-31b-it:free   # bake-off, each model alone
huntbot agents-eval --role team --profile glm          # false BUY must be 0 before enforcing
huntbot agents-eval --replay DIR / --record DIR        # offline fixtures
```

`evals/labels.csv` (ids and labels only) is committed; the scraped ad text is not. Switch provider with
`HUNTBOT_SCREEN_PROVIDER=openrouter|anthropic|rules` (default `auto`: OpenRouter if its key is set, else
Claude if `ANTHROPIC_API_KEY` is set, else rules). The bot never contacts sellers.

## Push notifications via GitHub Actions

`.github/workflows/hunt.yml` runs `huntbot run-once` every 30 minutes. It reads new Kleinanzeigen
alert mails, runs the eBay queries in `hunts.txt`, pushes buy signals to Telegram, and answers
queued Telegram commands (`/hunt`, `/stats` …; replies arrive with the next run). The database
persists in the Actions cache.

Add the repository secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IMAP_USER`, `IMAP_PASSWORD`,
`EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` and optionally `OPENROUTER_API_KEY` (screening team) or
`ANTHROPIC_API_KEY`. Then run it once via
*Actions → Deal hunt → Run workflow*.

- **Live Kleinanzeigen search is forced off there.** A schedule on GitHub's servers is exactly the
  automated polling the site forbids, and GitHub IPs get blocked quickly.
- **Minutes:** private repos get 2,000 free minutes/month, and every 30 min uses ≈ 1,450.
- **Speed:** GitHub can delay scheduled runs by several minutes. For seconds-fast alerts, run
  `huntbot serve` on a Pi or a €5 VM.

`.github/workflows/ci.yml` runs the tests and the Playwright e2e test on every push, and builds the
Claude skill as a downloadable artifact.

## Claude skill: "product-hunter"

`skills/product-hunter/` lets you ask Claude (claude.ai) things like "find me the best used
Sony A7 III under €900" or "is this ThinkPad a good flip?" for **any product**.

- Claude searches new prices (Geizhals/idealo) and sold prices on the web.
- It gives you Kleinanzeigen search links and analyses listings you paste. It doesn't fetch
  Kleinanzeigen itself.
- It prices with the same regression model (laptops/Dyson) or category depreciation curves
  (phones, cameras, consoles, tools, bikes …), computes flip profit, flags risks and drafts the offer.

```bash
python scripts/build_skill.py --db huntbot.db     # → dist/product-hunter.zip, with YOUR trained model
```

Upload the zip under claude.ai → Settings → Capabilities → Skills.

## MCP server (Claude Desktop / Claude Code)

To let Claude use your **local** bot (stored listings, eBay API, trained model):

```bash
pip install -e ".[mcp]"
claude mcp add huntbot -- huntbot mcp                      # Claude Code
```

For Claude Desktop, add this to `claude_desktop_config.json`:
`{"mcpServers": {"huntbot": {"command": "huntbot", "args": ["mcp"]}}}`.

Tools: `hunt`, `estimate_price`, `score_listing` (paste any listing), `top_deals`,
`add_sold_price`. None of them contacts sellers.

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
pytest                               # unit/integration tests, no network (the screening team is tested against a scripted OpenRouter)
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
