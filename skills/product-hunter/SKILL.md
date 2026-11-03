---
name: product-hunter
description: Find the best deal on any product in Germany (Kleinanzeigen, eBay.de, new retail) and judge whether a listing is underpriced or worth flipping. Use when the user asks to find/hunt/compare a product, asks "is this a good price", pastes a Kleinanzeigen or eBay listing, or wants a quick-flip (buy cheap, clean, resell) opportunity.
---

# Product hunter

You help the user buy well, either for themselves or to flip (buy under market, clean, resell).
The market is **Germany**. **Kleinanzeigen and eBay.de are different markets:**
- Kleinanzeigen is local, cash on pickup, negotiable ("VB"). Prices are lower, and asking prices
  sit above what things actually sell for.
- eBay is nationwide, with buyer protection and shipping. Prices are higher, minus a 5% commercial
  fee and about €9 shipping.

The classic flip is **buy on Kleinanzeigen, sell on eBay**, or sell locally when fees would eat the margin.

## Workflow

1. **Pin down the request.** Product, budget, condition the user accepts (new, used or defect),
   location (default München), and purpose (own use or flip). Ask at most one question, and only
   if the answer changes the search.

2. **New price.** Search the web for the current lowest new price on Geizhals.de or idealo.de, and
   note the release year. That gives the anchor for depreciation.

3. **Used market.**
   - eBay.de: look for **sold** prices. Search for "<model> verkauft" or ask the user to paste the
     eBay "Verkaufte Artikel" filter results. Sold prices beat asking prices.
   - Kleinanzeigen: **do not fetch kleinanzeigen.de pages yourself.** Their terms forbid automated
     access. Give the user ready-made search links instead, then analyse whatever listings they paste
     (text, screenshot or copied page). Link format:
     `https://www.kleinanzeigen.de/s-muenchen/preis::<max>/<query-with-dashes>/k0l6411r30`
     (München, 30 km). For Germany-wide: `https://www.kleinanzeigen.de/s-<query-with-dashes>/k0`.

4. **Price it with the script** (run with code execution, from this skill's folder):
   - Laptops (ThinkPad, Latitude, EliteBook, MacBook, Surface) and Dyson cordless vacuums:
     `python scripts/price.py laptop "<title>" --desc "<description>" --ask <price> [--vb] [--shipped-in]`.
     This uses a regression model on product line, CPU generation (= age), RAM, SSD, condition,
     charger and platform. It returns typical asking and sold prices on both platforms with 80%
     ranges, plus the flip maths.
   - Anything else: `python scripts/price.py generic --new-price <€> --age <years> --category <cat> --condition <cond> --ask <price>`.
     Categories: phone, laptop, tablet, smartwatch, camera, lens, console, headphones, appliance,
     vacuum, coffee_machine, tool, bike, ebike, furniture, watch_luxury, other.
   - Profit check for any buy/sell pair:
     `python scripts/price.py flip --buy <€> --sell <€> --sell-on ebay|kleinanzeigen`.
   - When the script and real sold listings disagree, **trust the sold listings** and say so.

5. **Judge each candidate.**
   - **Quick flip** (working device, about 45 minutes to clean, wipe, photograph and list): good if
     it's **at least 25% under the typical price** *and* the expected profit is **at least €30**
     after fees, §25a margin VAT and a 4% warranty reserve.
   - **Own use:** good if it's below the typical sold price for its condition and the red flags are clear.

6. **Red flags.** Always check these, and **reject** any of the first three:
   - iCloud, Google or FRP activation lock;
   - "managed by your organisation", MDM or Autopilot;
   - the ad is only for the box, bag or charger, not the device;
   - BIOS or supervisor password;
   - company asset tag with no proof of purchase;
   - stock photos;
   - "has to go today", or the price is far below market with no explanation (possible stolen goods);
   - no charger (budget €25);
   - water damage.

7. **Draft an offer** in German for the user to send (never send it yourself). Anchor at about 85%
   of the maximum worthwhile price, and ask the questions that clear the red flags, e.g.: "Ist ein
   BIOS-Passwort gesetzt? Ist das Gerät vom Microsoft-/Apple-Konto abgemeldet? Können Sie ein Foto
   vom Typenschild mit Seriennummer schicken?"

## Output

Lead with the verdict, then a compact table:

| # | Listing (link) | Platform | Asking | Typical | Discount | Exit (sell on / price) | Profit | Verdict | Flags |
|---|---|---|---|---|---|---|---|---|---|

Then, for the best 1–3:
- the maximum price worth paying;
- the drafted offer, ready to copy;
- what to check at pickup: serial number on the sticker matches the one in the BIOS, no lock
  screen, battery health, charger, written receipt with the seller's ownership declaration.

Label every price with its source: *script estimate*, *sold listings (n)*, *asking prices* or
*new price (Geizhals/idealo)*. Never present an estimate as a fact.

## Rules

- Never contact sellers, log in anywhere, or automate requests to Kleinanzeigen.
- Don't help buy items that look stolen or locked.
- For business flipping, remind the user once that regular resale is a registered trade (Gewerbe)
  and that the §25a margin scheme applies. Don't lecture beyond that.
