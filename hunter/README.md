# hunter: deal finder for repairing and reselling electronics

You describe what you want in plain language, for example *"ThinkPad T14 with a broken SSD or forgotten password, under €150, near Munich"*. The bot searches Kleinanzeigen and eBay, has Claude read each listing and its photos, and calculates the **expected profit after fees, §25a VAT, travel and failed repairs**. It ranks the results and can send the good ones to Telegram with a ready-to-paste offer.

It follows the MVP design in the consolidated plan (§4): Haiku 5.5 screens every listing, Sonnet 5.5 takes a second look at the promising ones, and the profit formula and buy rules come from §4.2. **The bot never messages sellers.** You send every message yourself.

```
your query ──► Haiku 5.5: German search terms + filters
                 │
   ┌─────────────┼───────────────────────────────┐
   ▼             ▼                               ▼
Kleinanzeigen   eBay Browse API (official)      Kleinanzeigen alert emails (IMAP)
live search     "for parts / not working"       ← your saved searches (watch mode)
(opt-in)
   └─────────────┴──────────────┬────────────────┘
                                ▼
     drop reposts, sold/reserved, accessories, excluded words, out-of-range
                                ▼
     Haiku 5.5 screen: text + up to 4 photos → model, fault, fix, parts €, p_ok, red flags
                                ▼
     profit score: EV, €/h, ROI, max buy price  (prices.yaml + §25a + warranty reserve)
                                ▼
     top N: read the full ad page → Sonnet 5.5 deep review (all photos) → offer draft
                                ▼
     ranked list in the terminal  ·  Telegram card  [Open ad] [Copy offer]
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-hunter.txt
python -m playwright install chromium      # only for Kleinanzeigen live search
cp .env.example .env                        # then fill in the hunter section
```

| What | Needed for | How |
|---|---|---|
| `ANTHROPIC_API_KEY` | query planning, photo screening, offers | console.anthropic.com. Without it the bot falls back to free keyword rules (`--no-claude`) |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | eBay "for parts" search | developer.ebay.com → Application Keys → **Production** |
| `HUNTER_IMAP_*` | watch mode (Tier 0) | A dedicated Gmail account with an app password that receives your Kleinanzeigen saved-search alerts |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | deal cards on your phone | @BotFather; chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `HUNTER_KA_LIVE=1` | Kleinanzeigen live search | **Read the next section first** |

## Kleinanzeigen live search is opt-in

Your plan decided against automating Kleinanzeigen (§2 #1). Its terms (§5) ban crawlers, including read-only ones, and it IP-blocks them. Live search is therefore **off** until you set `HUNTER_KA_LIVE=1`. That decision and its risk are yours. When it's on, the fetcher stays within the plan's Tier 1 limits:

- It runs only when **you** type a search. It never polls on a schedule (`watch` uses your alert emails instead).
- It loads at most one page every 30 s (`HUNTER_KA_MIN_INTERVAL_S`) and 100 pages a day (`HUNTER_KA_DAILY_CAP`). These limits are stored in SQLite, so separate runs share them.
- On a 403, 429 or CAPTCHA it **stops and pauses for 24 h**. It never logs in, never rotates proxies, never solves CAPTCHAs and never hides that it is automated.
- It doesn't download images. Claude reads the photos by URL.

A search with 3 query variants plus 3 ad pages therefore takes about 3 minutes. Use it from home, not from a server. Without live search, `search` still covers eBay, and `watch` covers Kleinanzeigen through your alert emails.

## Usage

```bash
# Hunt (Claude expands the query into several German searches)
python -m hunter search "thinkpad t14 oder t480 defekt, passwort vergessen oder ssd, max 150 euro"
python -m hunter search "dyson v11 akku defekt" --radius 30 --notify
python -m hunter search "latitude 5420 bastler" --no-kleinanzeigen     # eBay only
python -m hunter search "thinkpad defekt" --no-claude                  # free keyword rules

# Score one ad you found yourself (live, or from a page you saved with Ctrl+S)
python -m hunter score https://www.kleinanzeigen.de/s-anzeige/... --distance 8
python -m hunter score https://www.kleinanzeigen.de/s-anzeige/... --html ~/Downloads/ad.html

# Background: alert emails + eBay every 15 min → Telegram (no Kleinanzeigen requests)
python -m hunter watch --query "thinkpad t14" --query "latitude 54" --query "dyson v11" --max-price 150

# Track flips (feeds the realised €/h and the §25a record)
python -m hunter bought kleinanzeigen:3550598646 85 --serial PF2XXXXX
python -m hunter cost 1 --parts 45 --hours 1.5
python -m hunter sold 1 265
python -m hunter inventory
python -m hunter export exports/flips-2026.csv
```

Each result shows the listing, **EV** (expected profit), €/hour, ROI, the **maximum price worth paying**, the likely fix with its success probability, any red flags and, after a deep review, a German offer message. 🔥 marks listings that pass your buy rules: EV ≥ €40, ≥ €25/h and ROI ≥ 40% (`HUNTER_MIN_EV`, `HUNTER_MIN_EV_PER_HOUR`, `HUNTER_MIN_ROI`).

## How the score works (plan §4.2)

```
net_sale = resale_p50 − sale fee (5%) − shipping out − packaging − §25a VAT [(resale − buy) × 19/119] − 4% warranty reserve
cost_in  = buy (VB → ×0.9) + buyer fee if shipped (4.5% + €0.50) + parts (+€15 if no charger) + travel (2 × km × €0.30) or inbound shipping
EV       = p_ok × net_sale + (1 − p_ok) × salvage − cost_in
hours    = repair hours + 0.5 h overhead + driving time
```

- **Rejected outright:** activation lock, MDM/Autopilot, possibly stolen, accessory or part only, sold or reserved.
- **EV × 0.5:** BIOS password, or a company asset tag. **EV × 0.7:** stock photos only.
- **`p_ok` cap:** set to 0.9 until you've checked about 30 predictions against what you actually found (`HUNTER_P_OK_CAP`).
- **Pickup or shipping:** listings inside your radius are priced as pickup. Listings further away are priced as shipped, if the seller ships.
- **Small-business VAT:** `HUNTER_VAT_MODE=kleinunternehmer` removes the VAT line.

**`prices.yaml` holds estimates, not sold prices.** Before you trust the EV, replace each `resale_p50` with the median of 10 or more eBay *sold* results (plan §7, days 1–30). Add any model you want to hunt. Models are matched in file order, and the first match wins.

## Cost

- Haiku 5.5 screening: about $0.001 per listing with photos.
- Sonnet 5.5 deep review: about $0.03–0.05 each (up to 12 photos), only for the top few per search (`--deep`).
- A search over ~60 listings costs roughly **$0.10–0.25**.
- Running `watch` all day: **$5–25/month** (plan §4.3).
- Screenings are cached per listing and price, so re-running a search only pays for new or repriced ads.

## Data protection

Seller names and phone numbers are never parsed, stored or sent to Claude. Only the title, description, price, area and item photos are. Unbought listings are deleted after 60 days (`watch` does this automatically; otherwise run `purge`). Purchase and sale records stay, because they are bookkeeping (§147 AO). The database is `hunter.sqlite3` and is git-ignored.

## Tests

```bash
python -m pytest tests
```

Tests use anonymised fixtures and a fake Claude transport, so they need no network and no API key.

## Not built yet (from the plan)

- Telegram **Pass** and **Bought** buttons. These need a long-running bot process; for now use `python -m hunter bought`.
- A browser userscript for "score the ad I'm looking at". `score --html` covers this manually.
- Photo pHash for spotting duplicates. Reposts are currently caught by title, price and postcode.
- A listing generator for resale.
- The parser for alert emails is generic. Check it on a week of real alerts with `watch --once --dry-run`.
