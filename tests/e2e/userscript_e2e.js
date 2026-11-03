// End-to-end: real huntbot server + Playwright Chromium + the userscript on fixture pages.
// No request leaves the machine: kleinanzeigen.de and image hosts are answered by page.route().
// Run: node tests/e2e/userscript_e2e.js [screenshot-dir]
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const PORT = 8799, TOKEN = "e2e-token", API = `http://127.0.0.1:${PORT}`;
const shots = process.argv[2];
const fixture = (f) => fs.readFileSync(path.join(ROOT, "tests", "fixtures", f), "utf8");
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkqGcAAAIEAQDZy0bbAAAAAElFTkSuQmCC", "base64");
const assert = (cond, msg) => { if (!cond) throw new Error("ASSERT: " + msg); console.log("  ok -", msg); };

async function waitForServer() {
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(API + "/api/status")).ok) return; } catch (e) {}
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("server did not start");
}

async function withUserscript(page) {
  await page.exposeFunction("__gmxhr", async (opts) => {
    const res = await fetch(opts.url, { method: opts.method, headers: opts.headers, body: opts.data });
    return { status: res.status, responseText: await res.text() };
  });
  await page.addInitScript(({ api, token }) => {
    const store = { huntbot_api: api, huntbot_token: token };
    window.GM_getValue = (k, d) => (k in store ? store[k] : d);
    window.GM_setValue = (k, v) => { store[k] = v; };
    window.GM_registerMenuCommand = () => {};
    window.GM_xmlhttpRequest = (o) => window.__gmxhr({ url: o.url, method: o.method, headers: o.headers, data: o.data })
      .then((r) => o.onload && o.onload(r)).catch(() => o.onerror && o.onerror());
  }, { api: API, token: TOKEN });
  await page.route("https://www.kleinanzeigen.de/**", (route) => {
    const url = route.request().url();
    const body = url.includes("/s-anzeige/") ? fixture("ad_page.html") : fixture("search_page.html");
    route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body });
  });
  await page.route("https://img.example.test/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "huntbot-e2e-"));
  const server = spawn("python3", ["-m", "huntbot.cli", "serve", "--no-email", "--no-telegram", "--port", String(PORT)], {
    cwd: tmp, // no .env here
    env: { ...process.env, PYTHONPATH: ROOT, HUNTBOT_DB: path.join(tmp, "e2e.db"), HUNTBOT_TOKEN: TOKEN,
           HUNTBOT_LLM_ENABLED: "false", EBAY_CLIENT_ID: "", TELEGRAM_BOT_TOKEN: "", IMAP_USER: "" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = ""; server.stdout.on("data", (d) => (log += d)); server.stderr.on("data", (d) => (log += d));
  const browser = await chromium.launch();
  const userscript = fs.readFileSync(path.join(ROOT, "userscript", "huntbot.user.js"), "utf8");
  try {
    await waitForServer();
    const ctx = await browser.newContext({ viewport: { width: 1000, height: 760 }, deviceScaleFactor: shots ? 2 : 1 });
    const page = await ctx.newPage();
    await withUserscript(page);

    console.log("search results page");
    await page.goto("https://www.kleinanzeigen.de/s-muenchen/preis::150/thinkpad-defekt/k0l6411r30");
    await page.addScriptTag({ content: userscript });
    await page.waitForFunction(() => document.querySelectorAll(".huntbot-badge").length === 3, null, { timeout: 15000 });
    const badges = await page.$$eval("article.aditem", (as) => as.map((a) => [a.dataset.adid, a.querySelector(".huntbot-badge").textContent.replace(/\s+/g, " ").trim()]));
    const byId = Object.fromEntries(badges);
    console.log("   ", badges);
    assert(/^EV €\d+/.test(byId["2901234567"]) && parseInt(byId["2901234567"].match(/EV €(\d+)/)[1]) >= 40, "T14 G2 'Windows Fehler' scores EV ≥ €40");
    assert(byId["2907654321"].startsWith("REJECT"), "iCloud-locked listing is rejected");
    assert(/activation_lock/.test(byId["2907654321"]), "rejection shows the activation_lock flag");
    assert(/EV €/.test(byId["2905555555"]), "Latitude 5420 with broken screen gets a score");
    if (shots) await page.screenshot({ path: path.join(shots, "userscript-search.png") });

    console.log("single ad page");
    await page.goto("https://www.kleinanzeigen.de/s-anzeige/hp-elitebook-840-g8-akku-defekt/2933333333-278-6411");
    await page.addScriptTag({ content: userscript });
    await page.waitForSelector(".huntbot-badge button", { timeout: 15000 });
    const adBadge = (await page.textContent(".huntbot-badge")).replace(/\s+/g, " ");
    assert(/EV €\d+/.test(adBadge), "ad page shows EV badge: " + adBadge.trim().slice(0, 60));

    const stored = await (await fetch(`${API}/api/top?limit=10`, { headers: { "X-Huntbot-Token": TOKEN } })).json();
    const ad = stored.find((s) => s.key === "kleinanzeigen:2933333333");
    assert(ad && ad.price === 120, "ad page listing stored with price");
    const db = fs.readFileSync(path.join(tmp, "e2e.db")).toString("latin1") + (fs.existsSync(path.join(tmp, "e2e.db-wal")) ? fs.readFileSync(path.join(tmp, "e2e.db-wal")).toString("latin1") : "");
    assert(!db.includes("0176 1234567") && !db.includes("Erika"), "phone number and seller name are not stored");

    console.log("web search UI");
    const ui = await ctx.newPage();
    await ui.goto(API + "/");
    await ui.click("details summary");
    await ui.fill("#token", TOKEN); await ui.dispatchEvent("#token", "change");
    await ui.fill("#q", "thinkpad bis 150");
    await ui.click("#go");
    await ui.waitForSelector(".card", { timeout: 15000 });
    const cards = await ui.$$eval(".card", (cs) => cs.map((c) => [c.className, c.querySelector(".title").textContent, c.querySelector(".ev").textContent]));
    console.log("   ", cards);
    assert(cards[0][1].includes("T14 Gen 2") && cards[0][0].includes("alert"), "best deal (T14 G2) ranked first and highlighted");
    assert(cards[cards.length - 1][0].includes("rejected"), "rejected listing sorted last");
    assert(await ui.$$eval(".links a", (as) => as.some((a) => a.href.includes("kleinanzeigen.de/s-muenchen/preis::150/"))), "Kleinanzeigen search links generated");
    if (shots) await ui.screenshot({ path: path.join(shots, "web-ui.png"), fullPage: true });
    console.log("\nE2E PASSED");
  } catch (e) {
    console.error(e.message, "\n--- server log ---\n", log);
    process.exitCode = 1;
  } finally {
    await browser.close();
    server.kill();
  }
})();
