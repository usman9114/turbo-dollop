// ==UserScript==
// @name         Huntbot – deal scores on Kleinanzeigen
// @namespace    huntbot.local
// @version      0.1.0
// @description  Reads the listings on the Kleinanzeigen page YOU opened, sends them to your local huntbot, and shows expected profit on each ad. No clicks, no navigation, no messages.
// @match        https://www.kleinanzeigen.de/s-*
// @match        https://www.kleinanzeigen.de/s-anzeige/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const API = () => GM_getValue("huntbot_api", "http://127.0.0.1:8787");
  const TOKEN = () => GM_getValue("huntbot_token", "");

  GM_registerMenuCommand("Huntbot: set API token", () => {
    const t = prompt("HUNTBOT_TOKEN from your .env", TOKEN());
    if (t !== null) GM_setValue("huntbot_token", t.trim());
  });
  GM_registerMenuCommand("Huntbot: set server URL", () => {
    const u = prompt("Huntbot server", API());
    if (u) GM_setValue("huntbot_api", u.trim().replace(/\/$/, ""));
  });

  // ------------------------------------------------------------ extraction
  // Selectors are best-effort (Kleinanzeigen markup changes); each has a generic fallback.
  const text = (root, sel) => {
    const el = sel ? root.querySelector(sel) : root;
    return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
  };
  const absUrl = (href) => { try { return new URL(href, location.origin).href; } catch (e) { return ""; } };
  const imgSrc = (img) => {
    if (!img) return "";
    const src = img.getAttribute("src") || img.getAttribute("data-src") || img.getAttribute("data-imgsrc") || "";
    const set = img.getAttribute("srcset");
    const best = set ? set.split(",").pop().trim().split(" ")[0] : "";
    const url = absUrl(best || src);
    return url.startsWith("https://") ? url : "";
  };
  const AD_RE = /\/s-anzeige\/[^/]+\/(\d{6,})/;

  function extractSearchPage(doc) {
    const items = [];
    const articles = doc.querySelectorAll("article.aditem, article[data-adid], li.ad-listitem article");
    articles.forEach((a) => {
      const link = a.querySelector("a[href*='/s-anzeige/']");
      const href = a.getAttribute("data-href") || (link && link.getAttribute("href")) || "";
      const url = absUrl(href);
      const m = url.match(AD_RE);
      const adId = a.getAttribute("data-adid") || (m && m[1]);
      if (!adId) return;
      items.push({
        ad_id: adId,
        url,
        title: text(a, "h2 a, .text-module-begin a, a.ellipsis") || (link ? text(link) : ""),
        price_text: text(a, ".aditem-main--middle--price-shipping--price, [class*='price-shipping--price'], [class*='price']"),
        shipping_text: text(a, ".aditem-main--middle--price-shipping--shipping, [class*='shipping--shipping']"),
        location: text(a, ".aditem-main--top--left, [class*='top--left']"),
        description: text(a, ".aditem-main--middle--description, [class*='description']"),
        images: [imgSrc(a.querySelector(".imagebox img, .aditem-image img, img"))].filter(Boolean),
        _el: a,
      });
    });
    return items;
  }

  function extractAdPage(doc, href) {
    const m = href.match(AD_RE);
    if (!m) return [];
    const imgs = Array.from(doc.querySelectorAll("#viewad-image, .galleryimage-element img, [class*='gallery'] img"))
      .map(imgSrc).filter(Boolean);
    return [{
      ad_id: m[1],
      url: href.split("?")[0],
      title: text(doc, "#viewad-title, h1"),
      price_text: text(doc, "#viewad-price, [class*='boxedarticle--price']"),
      shipping_text: text(doc, ".boxedarticle--details--shipping, [class*='details--shipping']"),
      location: text(doc, "#viewad-locality"),
      description: text(doc, "#viewad-description-text, [itemprop='description']"),
      seller: text(doc, ".userprofile-vip a, [class*='userprofile'] a"), // hashed server-side, never stored in clear
      images: Array.from(new Set(imgs)).slice(0, 10),
      _el: doc.querySelector("#viewad-price, #viewad-title, h1"),
    }];
  }

  // ------------------------------------------------------------ rendering
  const eur = (v) => (v == null ? "?" : "€" + Math.round(v));
  function badgeHtml(r) {
    const color = r.rejected ? "#b3261e" : r.alert ? "#1f7a4d" : r.ev > 0 ? "#a15c00" : "#6b6a66";
    const label = r.rejected ? "REJECT" : `EV ${eur(r.ev)}`;
    const flags = (r.flags || []).join(", ");
    return `<span style="background:${color};color:#fff;border-radius:6px;padding:2px 8px;font:600 12px system-ui">${label}</span>
      <span style="font:12px system-ui;color:${color};margin-left:6px">${r.rejected ? "" : `${eur(r.ev_per_hour)}/h · max buy ${eur(r.max_buy)} · `}
      ${flags ? "🚩 " + flags.replace(/[<>&]/g, "") : ""}</span>`;
  }

  function render(items, results, isAd) {
    items.forEach((it) => {
      const r = results[it.ad_id];
      if (!r || !it._el) return;
      let box = it._el.querySelector ? it._el.querySelector(":scope > .huntbot-badge") : null;
      if (!box) {
        box = document.createElement("div");
        box.className = "huntbot-badge";
        box.style.cssText = "margin:4px 0;display:block";
        if (isAd) it._el.insertAdjacentElement("afterend", box);
        else it._el.prepend(box);
      }
      box.innerHTML = badgeHtml(r);
      box.title = `${r.model} — ${r.fault}\n${(r.reasons || []).join("; ")}\n(${r.model_used})`;
      if (isAd && r.offer && !box.querySelector("button")) {
        const b = document.createElement("button");
        b.textContent = "Copy offer";
        b.style.cssText = "margin-left:8px;font:12px system-ui;padding:2px 8px";
        b.addEventListener("click", (e) => { e.preventDefault(); navigator.clipboard.writeText(r.offer); b.textContent = "Copied"; });
        box.appendChild(b);
      }
    });
  }

  // ------------------------------------------------------------ transport
  const sent = new Set();
  function send(page, items) {
    const fresh = items.filter((i) => !sent.has(i.ad_id + "|" + i.price_text + "|" + i.description.length));
    if (!fresh.length) return;
    fresh.forEach((i) => sent.add(i.ad_id + "|" + i.price_text + "|" + i.description.length));
    const payload = { page, items: fresh.map(({ _el, ...rest }) => rest) };
    GM_xmlhttpRequest({
      method: "POST",
      url: API() + "/api/ingest",
      headers: { "Content-Type": "application/json", "X-Huntbot-Token": TOKEN() },
      data: JSON.stringify(payload),
      timeout: 120000,
      onload: (res) => {
        if (res.status !== 200) { console.warn("[huntbot]", res.status, res.responseText); return; }
        render(fresh, JSON.parse(res.responseText).results || {}, page === "ad");
      },
      onerror: () => console.warn("[huntbot] server not reachable at " + API()),
    });
  }

  function scan() {
    if (AD_RE.test(location.pathname)) send("ad", extractAdPage(document, location.href));
    else send("search", extractSearchPage(document));
  }

  // Exposed for tests.
  window.__huntbot = { extractSearchPage, extractAdPage, scan };

  scan();
  let timer = null;
  new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(scan, 800); })
    .observe(document.body, { childList: true, subtree: true });
})();
