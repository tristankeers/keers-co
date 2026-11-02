// Keers & Co research workbench. Plain ES module, no build step.
// Display only: every number shown comes from the API, which reads the store and the versioned engines.
// This file formats numbers; it never calculates financial figures.

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");

// --- tiny DOM helper: strings always go in as text, never as HTML -------------------------------------------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
// replaceChildren without the nulls that conditional children produce (it would print them as "null").
function fill(el, ...kids) {
  if (el) el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}
const extLink = (href, ...kids) =>
  href ? h("a", { href, target: "_blank", rel: "noopener noreferrer nofollow" }, ...kids) : h("span", {}, ...kids);

// Every request has a time limit, so a slow source shows an error with a Retry button instead of hanging.
async function api(path, opts = {}) {
  const { timeout = 45000, signal, ...rest } = opts;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(new DOMException("timeout", "TimeoutError")), timeout);
  if (signal) signal.addEventListener("abort", () => ctl.abort(signal.reason), { once: true });
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...rest, signal: ctl.signal });
  } catch (e) {
    if (ctl.signal.reason?.name === "TimeoutError") throw new Error(`no answer from the server after ${Math.round(timeout / 1000)}s (a live source may be slow; check the Terminal running python -m keers.web)`);
    if (e.name === "AbortError") throw e;
    throw new Error(`could not reach the server (${e.message}). Is python -m keers.web still running?`);
  } finally { clearTimeout(timer); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `HTTP ${res.status}`);
  return body;
}

// --- formatting ----------------------------------------------------------------------------------------------
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const nf = (dp) => new Intl.NumberFormat("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const NF = {};
const nfd = (dp) => (NF[dp] ??= nf(dp));
function fnum(v, dp = 1) { return isNum(v) ? nfd(dp).format(v) : "—"; }
function fpct(v, dp = 1) { return isNum(v) ? `${nfd(dp).format(v * 100)}%` : "—"; }
function fx(v, dp = 2) { return isNum(v) ? `${nfd(dp).format(v)}x` : "—"; }
function signed(s, v) { return isNum(v) && v > 0 ? `+${s}` : s; }

// Units used by the data layer: USDm, USD, ratio, %, x, "m shares".
function fval(v, unit) {
  if (!isNum(v)) return "—";
  if (/^[A-Z]{3}m$/.test(unit || "")) return fnum(v, Math.abs(v) >= 100 ? 0 : 1);  // millions of a currency
  switch (unit) {
    case "USDm": return fnum(v, Math.abs(v) >= 100 ? 0 : 1);
    case "USD": return fnum(v, 2);
    case "ratio": case "pct": return fpct(v, 1);
    case "%": return `${fnum(v, 2)}%`;
    case "x": return fx(v, 2);
    case "m shares": return fnum(v, 1);
    case "score": return fnum(v, 0);
    default: return fnum(v, 2);
  }
}
function unitText(unit, currency) {
  const m = /^([A-Z]{3})m$/.exec(unit || "");
  if (m) return `${m[1]} m`;
  return { USDm: "USD m", USD: currency || "USD", ratio: "", "%": "", x: "", "m shares": "m shares", pct: "" }[unit] ?? (unit || "");
}
function fdate(iso) { return iso ? String(iso).slice(0, 10) : "—"; }
function fdatetime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

// --- labels --------------------------------------------------------------------------------------------------
const LABEL_TEXT = {
  reported: "As filed with the SEC.",
  derived: "Calculated in code from reported figures. The method says how.",
  market: "A market price or rate, with its date and source.",
  assumption: "A model input: a calculation version's default, or your edit.",
  estimate: "A model output. Only as good as its assumptions.",
  stale: "Older than the freshness rules allow. Do not rely on it.",
};
function chip(label) { return h("span", { class: `chip ${label}`, title: LABEL_TEXT[label] || "" }, label); }
function chips(fig) { return [fig.label ? chip(fig.label) : null, fig.stale ? chip("stale") : null]; }
function legend() {
  return h("div", { class: "legend" }, Object.entries(LABEL_TEXT).map(([k, t]) => h("span", {}, chip(k), t)));
}
function levelBadge(lvl) { return h("span", { class: `lvl ${String(lvl).replace(/\s+/g, "-")}` }, lvl); }

// --- provenance drawer ---------------------------------------------------------------------------------------
const drawer = $("#drawer");
$("#drawer-close").addEventListener("click", () => closeDrawer());
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
function closeDrawer() { drawer.hidden = true; document.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel")); }
function openDrawer(title, fig, currency) {
  const body = $("#drawer-body");
  fill(body, 
    h("h3", {}, title),
    h("div", { class: "big" }, fval(fig.value, fig.unit), " ", h("small", { class: "muted" }, unitText(fig.unit, currency))),
    h("div", {}, chips(fig)),
    h("dl", {},
      h("dt", {}, "Label"), h("dd", {}, LABEL_TEXT[fig.label] || fig.label || "—"),
      h("dt", {}, "Method"), h("dd", {}, fig.method || "—"),
      h("dt", {}, "Period end"), h("dd", { class: "mono" }, fdate(fig.periodEnd)),
    ),
    (fig.warnings || []).length ? h("div", { class: "callout warn" }, h("span", { class: "eyebrow" }, "Warnings"), h("ul", {}, fig.warnings.map((w) => h("li", {}, w)))) : null,
    h("div", { class: "eyebrow", style: "margin-top:14px" }, `Sources (${(fig.sources || []).length})`),
    (fig.sources || []).length ? fig.sources.map((s) => h("div", { class: "src" },
      h("div", {}, s.text || s.source || ""),
      s.value !== undefined && s.unit ? h("div", { class: "mono muted" }, `${fnum(s.value, 0)} ${s.unit}`) : null,
      s.accn ? h("div", { class: "mono muted" }, `Accession ${s.accn}`) : null,
      s.link ? h("div", {}, extLink(s.link, s.accn ? "Open the filing on SEC EDGAR" : "Open the source")) : null,
      s.raw_sha256 ? h("div", { class: "hash" }, `raw sha256 ${s.raw_sha256}`) : null,
    )) : h("p", { class: "muted" }, "No source documents: this figure is missing or computed from other figures shown alongside it."),
  );
  drawer.hidden = false;
}

// --- status bar ----------------------------------------------------------------------------------------------
let STATUS = null;
async function loadStatus() {
  try {
    STATUS = await api("/api/status");
    const sys = $("#sys");
    fill(sys, 
      h("span", {}, h("span", { class: `dot ${STATUS.live ? "on" : "off"}` }), STATUS.live ? "Live sources on" : "Offline: store only"),
      h("span", {}, h("span", { class: `dot ${STATUS.secEnabled ? "on" : "off"}` }), STATUS.secEnabled ? "SEC on" : "SEC off"),
      h("span", { class: "mono" }, STATUS.today),
    );
    $("#notice").textContent = STATUS.notice;
  } catch (e) {
    $("#sys").textContent = `API unavailable: ${e.message}`;
  }
}

// --- search --------------------------------------------------------------------------------------------------
// Suggestions come from the local directories (universe, store, SEC map) as you type; Yahoo's live search is
// asked a moment later and only adds listings the local ones lack.
const q = $("#q"), results = $("#results");
const SYMBOL_RE = /^[A-Za-z0-9.\-=^]{1,24}$/;
let searchTimer = null, remoteTimer = null, searchSeq = 0, hits = [], remoteHits = [], sel = -1;
let localPending = null, remoteState = "idle", lastSources = [];
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== q && !/INPUT|TEXTAREA/.test(document.activeElement?.tagName)) { e.preventDefault(); q.focus(); q.select(); }
});
q.addEventListener("input", () => {
  clearTimeout(searchTimer); clearTimeout(remoteTimer);
  searchTimer = setTimeout(runSearch, 70);
});
const allHits = () => hits.concat(remoteHits);
q.addEventListener("keydown", async (e) => {
  const n = allHits().length;
  if (e.key === "ArrowDown") { e.preventDefault(); if (n) { sel = (sel + 1) % n; paintHits(); } }
  else if (e.key === "ArrowUp") { e.preventDefault(); if (n) { sel = sel <= 0 ? n - 1 : sel - 1; paintHits(); } }
  else if (e.key === "Tab" && !results.hidden && allHits()[sel]) { e.preventDefault(); q.value = allHits()[sel].symbol; runSearch(); }
  else if (e.key === "Enter") {
    e.preventDefault();
    const text = q.value.trim();
    if (!text) return;
    // Pressed before the suggestions arrived: wait for them rather than guessing.
    if (searchTimer || localPending) { clearTimeout(searchTimer); searchTimer = null; await (localPending || runSearch()); }
    const pick = allHits()[sel] || allHits()[0];
    if (pick) return go(pick.symbol);
    if (SYMBOL_RE.test(text) && !/\s/.test(text)) return go(text.toUpperCase());
    paintHits(); // "no match" message; a name with spaces is never opened as a ticker
  } else if (e.key === "Escape") { results.hidden = true; q.blur(); }
});
q.addEventListener("blur", () => setTimeout(() => (results.hidden = true), 150));
q.addEventListener("focus", () => { if (q.value.trim() && (allHits().length || remoteState !== "idle")) results.hidden = false; });

function runSearch() {
  searchTimer = null;
  const text = q.value.trim();
  const seq = ++searchSeq;
  remoteHits = []; remoteState = "idle";
  if (!text) { hits = []; sel = -1; results.hidden = true; return Promise.resolve(); }
  localPending = (async () => {
    try {
      const r = await api(`/api/search?q=${encodeURIComponent(text)}&limit=12`, { timeout: 10000 });
      if (seq !== searchSeq) return;
      hits = r.results; lastSources = r.sources || []; sel = hits.length ? 0 : -1;
      if (STATUS?.live && text.length >= 2) { remoteState = "waiting"; remoteTimer = setTimeout(() => runRemote(text, seq), 350); }
      paintHits();
    } catch (e) {
      if (seq !== searchSeq) return;
      hits = []; fill(results, h("li", { class: "empty" }, `Search failed: ${e.message}`)); results.hidden = false;
    } finally { if (seq === searchSeq) localPending = null; }
  })();
  return localPending;
}
async function runRemote(text, seq) {
  if (seq !== searchSeq) return;
  remoteState = "loading"; paintHits();
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(text)}&limit=8&remote=1`, { timeout: 20000 });
    if (seq !== searchSeq) return;
    remoteHits = r.results; remoteState = "done";
    if (sel < 0 && remoteHits.length) sel = 0;
  } catch { if (seq !== searchSeq) return; remoteState = "failed"; }
  paintHits();
}
// Show the typed text in bold where it matches the symbol or name.
function marked(text, query) {
  const t = String(text || ""), words = query.trim().toUpperCase().split(/[\s\-,./&]+/).filter(Boolean);
  if (!t || !words.length) return t || "—";
  const up = t.toUpperCase(), on = new Array(t.length).fill(false);
  for (const w of words) {
    let i = up.indexOf(w);
    while (i >= 0) { if (i === 0 || /[\s\-,./&(]/.test(up[i - 1])) { for (let k = i; k < i + w.length; k++) on[k] = true; break; } i = up.indexOf(w, i + 1); }
  }
  const out = []; let i = 0;
  while (i < t.length) { let j = i; while (j < t.length && on[j] === on[i]) j++; out.push(on[i] ? h("mark", {}, t.slice(i, j)) : t.slice(i, j)); i = j; }
  return out;
}
function hitRow(x, i, text) {
  return h("li", { role: "option", id: `hit-${i}`, "aria-selected": i === sel ? "true" : "false",
    onmousedown: (e) => { e.preventDefault(); go(x.symbol); }, onmousemove: () => { if (sel !== i) { sel = i; paintHits(); } } },
    h("span", { class: "sym" }, marked(x.symbol, text)),
    h("span", { class: "nm" }, marked(x.name, text)),
    h("span", { class: "meta" }, [x.exchange, x.country].filter(Boolean).join(" · "), " ",
      (x.warnings || []).length ? h("span", { title: x.warnings.join("\n") }, "⚠ ") : null,
      h("span", { class: "chip cov", title: x.coverageText }, x.coverage)));
}
function paintHits() {
  const text = q.value.trim();
  if (!text) { results.hidden = true; return; }
  const rows = hits.map((x, i) => hitRow(x, i, text));
  if (remoteHits.length) {
    rows.push(h("li", { class: "group" }, "More listings from Yahoo search (live)"));
    rows.push(...remoteHits.map((x, i) => hitRow(x, hits.length + i, text)));
  }
  if (!allHits().length) {
    const asSym = SYMBOL_RE.test(text) && !/\s/.test(text);
    rows.push(h("li", { class: "empty" }, remoteState === "waiting" || remoteState === "loading" ? "No local match yet." :
      asSym ? `No match. Press Enter to open "${text.toUpperCase()}" as a ticker.` : "No match. Try fewer words, or the ticker."));
  }
  if (remoteState === "loading") rows.push(h("li", { class: "empty faint" }, "Searching Yahoo for more listings…"));
  else if (remoteState === "failed") rows.push(h("li", { class: "empty faint" }, "Yahoo search did not answer; local results only."));
  rows.push(h("li", { class: "empty faint keys" }, `↑↓ choose · Enter open · Tab fill · Esc close · searched: ${lastSources.concat(remoteState === "done" ? ["yahoo"] : []).join(", ")}`));
  fill(results, rows);
  results.hidden = false;
  q.setAttribute("aria-activedescendant", sel >= 0 ? `hit-${sel}` : "");
  $(`#hit-${sel}`, results)?.scrollIntoView({ block: "nearest" });
}
function go(symbol) {
  clearTimeout(searchTimer); clearTimeout(remoteTimer); searchSeq++;
  results.hidden = true; q.value = ""; hits = []; remoteHits = []; sel = -1; remoteState = "idle";
  location.hash = `#/c/${encodeURIComponent(symbol)}`;
}

// --- router --------------------------------------------------------------------------------------------------
const TABS = [["overview", "Overview"], ["valuation", "Valuation"], ["distress", "Distress screen"], ["chart", "Chart"], ["news", "News"], ["research", "Research memo"]];
let current = { symbol: null, company: null };
let chartHandle = null;

async function route() {
  closeDrawer();
  if (chartHandle) { chartHandle.remove(); chartHandle = null; }
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const section = parts[0] === "portfolio" ? "#/portfolio" : "#/";
  document.querySelectorAll(".nav a").forEach((a) => a.setAttribute("aria-current", a.getAttribute("href") === section ? "page" : "false"));
  if (parts[0] === "c" && parts[1]) {
    const symbol = parts[1].toUpperCase();
    const tab = TABS.some(([k]) => k === parts[2]) ? parts[2] : "overview";
    return companyView(symbol, tab);
  }
  current = { symbol: null, company: null };
  if (parts[0] === "portfolio") return portfolioView(parts[1], parts[2]);
  return homeView();
}
window.addEventListener("hashchange", route);

// --- home ----------------------------------------------------------------------------------------------------
async function homeView() {
  document.title = "Keers & Co Workbench";
  fill(view, h("div", { class: "loading" }, "Loading…"));
  await loadStatus();
  const s = STATUS || { companies: [], runs: [], rates: {} };
  fill(view, 
    h("div", { class: "home-hero" },
      h("h1", {}, "Research workbench"),
      h("p", {}, "Search any listed company. US SEC filers get the full view: filed fundamentals, the kci-v3 valuation with its kci-v1 comparison, and the distress screen. Other listings show price, chart and news until a fundamentals source exists for their market."),
    ),
    h("div", { class: "grid cols-2" },
      h("section", { class: "panel" },
        h("div", { class: "ph" }, h("h2", {}, `Companies with filings in the store (${s.companies.length})`)),
        h("div", { class: "pb flush scroll-x" }, s.companies.length ? h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Ticker"), h("th", {}, "Name"), h("th", { class: "n" }, "CIK"), h("th", { class: "n" }, "Updated"))),
          h("tbody", {}, s.companies.map((c) => h("tr", { class: "clickable", onclick: () => go(c.ticker) },
            h("td", { class: "mono" }, h("a", { href: `#/c/${encodeURIComponent(c.ticker)}` }, c.ticker)), h("td", {}, c.name),
            h("td", { class: "n" }, c.cik), h("td", { class: "n" }, fdate(c.updatedAt))))),
        ) : h("p", { class: "muted", style: "padding:12px" }, "Nothing loaded yet. Search for a company and press Refresh on its page.")),
      ),
      h("div", { class: "stack" },
        h("section", { class: "panel" },
          h("div", { class: "ph" }, h("h2", {}, "How figures are labelled")),
          h("div", { class: "pb" }, legend()),
        ),
        h("section", { class: "panel" },
          h("div", { class: "ph" }, h("h2", {}, "Data status")),
          h("div", { class: "pb" },
            h("table", {}, h("tbody", {},
              h("tr", {}, h("td", {}, "Live sources"), h("td", { class: "n" }, s.live ? "on" : "off (store only)")),
              h("tr", {}, h("td", {}, "SEC EDGAR"), h("td", { class: "n" }, s.secEnabled ? "on" : "off: set SEC_USER_AGENT")),
              h("tr", {}, h("td", {}, "Price sources, in order"), h("td", { class: "n mono" }, (s.priceSources || []).join(" → ") || "—")),
              h("tr", {}, h("td", {}, "Company directory"), h("td", { class: "n mono" }, (s.directory || []).join(" → "))),
              h("tr", {}, h("td", {}, "10-year Treasury (FRED DGS10)"), h("td", { class: "n mono" }, s.rates?.DGS10 ? `${fnum(s.rates.DGS10[0], 2)}% on ${s.rates.DGS10[1]}` : "not loaded")),
              h("tr", {}, h("td", {}, "Baa spread (FRED BAA10Y)"), h("td", { class: "n mono" }, s.rates?.BAA10Y ? `${fnum(s.rates.BAA10Y[0], 2)}pp on ${s.rates.BAA10Y[1]}` : "not loaded")),
              h("tr", {}, h("td", {}, "Calculation version"), h("td", { class: "n mono" }, `${s.defaultCalcVersion} (compared with kci-v1)`)),
              h("tr", {}, h("td", {}, "Distress screen"), h("td", { class: "n mono" }, s.screenVersion)),
            )),
            s.runs?.length ? h("details", { style: "margin-top:10px" }, h("summary", {}, `Recent refresh runs (${s.runs.length})`),
              h("table", {}, h("tbody", {}, s.runs.map((r) => h("tr", {}, h("td", { class: "mono" }, fdate(r.started_at)), h("td", {}, r.kind), h("td", {}, r.status),
                h("td", { class: "muted" }, Object.keys(r.detail?.failed || {}).length ? `failed: ${Object.keys(r.detail.failed).join(", ")}` : "")))))) : null,
          ),
        ),
      ),
    ),
  );
}

// --- company shell ------------------------------------------------------------------------------------------
let loadSeq = 0;
async function companyView(symbol, tab) {
  if (current.symbol !== symbol || !current.company) {
    if (!SYMBOL_RE.test(symbol)) {
      // e.g. a company name in the address: search for it instead of asking the server for a ticker that can't exist
      fill(view, h("div", { class: "callout warn" }, `"${symbol}" is not a ticker. `,
        h("button", { class: "btn", onclick: () => { location.hash = ""; q.value = symbol; q.focus(); runSearch(); } }, `Search for "${symbol}"`)));
      return;
    }
    const seq = ++loadSeq, started = Date.now();
    const note = h("span", { class: "faint" });
    fill(view, h("div", { class: "loading" }, `Loading ${symbol}… `, note));
    const tick = setInterval(() => {
      const s = Math.round((Date.now() - started) / 1000);
      note.textContent = s >= 3 ? `${s}s${s >= 10 ? " (asking the live sources; this can take up to 45s)" : ""}` : "";
    }, 1000);
    try {
      const company = await api(`/api/companies/${encodeURIComponent(symbol)}`, { timeout: 45000 });
      if (seq !== loadSeq) return;
      current = { symbol, company };
    } catch (e) {
      if (seq !== loadSeq) return;
      fill(view, h("div", { class: "callout bad" }, h("strong", {}, `Could not load ${symbol}. `), e.message,
        h("div", { style: "margin-top:10px;display:flex;gap:8px" },
          h("button", { class: "btn primary", onclick: () => companyView(symbol, tab) }, "Retry"),
          h("a", { class: "btn", href: "#/" }, "Back to search"))));
      return;
    } finally { clearInterval(tick); }
  }
  const c = current.company;
  document.title = `${c.symbol} · Keers & Co`;
  const body = h("div", {});
  const bar = tabsBar(c, tab);
  fill(view, companyHeader(c), bar, body);
  if (!STATUS) await loadStatus();
  if (current.company === c && needsAutoLoad(c)) autoLoad(c, tab, $("#refresh-btn", bar));
  const render = { overview: overviewTab, valuation: valuationTab, distress: distressTab, chart: chartTab, news: newsTab, research: researchTab }[tab];
  try { await render(c, body); } catch (e) { fill(body, h("p", { class: "err" }, e.message)); }
  if (tab === "overview" || tab === "chart") refreshQuote(c);
}

function companyHeader(c) {
  const l = c.listing || {};
  const name = l.name || c.sec?.name || "";
  return h("div", { class: "co-head" },
    h("div", { class: "co-id" },
      h("div", {}, h("span", { class: "ticker" }, c.symbol), h("span", { class: "name" }, name || "Unknown listing")),
      h("div", { class: "facts" },
        l.exchange ? h("span", {}, l.exchange) : null,
        l.country ? h("span", {}, l.country) : null,
        l.currency ? h("span", {}, l.currency) : null,
        c.sec ? h("span", {}, "CIK ", extLink(c.sec.link, String(c.sec.cik))) : null,
        c.sec?.sic ? h("span", {}, `SIC ${c.sec.sic}`) : null,
        c.sec?.fiscalYearEnd ? h("span", {}, `FY ends ${c.sec.fiscalYearEnd.slice(0, 2)}/${c.sec.fiscalYearEnd.slice(2)}`) : null,
        c.filer ? h("span", {}, "LEI ", extLink(c.filer.link, c.filer.identifier)) : null,
        c.link ? h("span", { class: "chip", title: c.link.detail }, `accounts linked: ${c.link.basis}`) : null,
        c.snapshot && c.snapshot.currency && c.snapshot.currency !== "USD" ? h("span", {}, `accounts in ${c.snapshot.currency}`) : null,
        h("span", { class: "chip cov", title: l.coverageText || "" }, l.coverage || "unknown coverage"),
        h("span", { class: "faint" }, `directory: ${l.source || "—"}`),
      ),
    ),
    h("div", { class: "co-px", id: "co-px" }, priceBlock(c)),
    (l.warnings || []).length ? h("div", { class: "callout warn", style: "flex-basis:100%" }, h("strong", {}, "Listing record: "), l.warnings.join(" · ")) : null,
  );
}

function priceBlock(c) {
  const qt = c.quote;
  if (qt) {
    return [
      h("div", { class: "px" }, fnum(qt.price, 2), h("small", {}, qt.currency || "")),
      h("div", { class: "when" }, `${qt.source} · last trade ${fdatetime(qt.at)} `, chip("market"), qt.stale ? chip("stale") : null),
      (qt.warnings || []).length ? h("div", { class: "when" }, qt.warnings.join(" · ")) : null,
    ];
  }
  if (c.lastClose) {
    return [h("div", { class: "px" }, fnum(c.lastClose.value, 2), h("small", {}, c.listing?.currency || "")),
      h("div", { class: "when" }, `${c.lastClose.method} `, chip("market"), c.lastClose.stale ? chip("stale") : null)];
  }
  return [h("div", { class: "when" }, "No price loaded")];
}

async function refreshQuote(c) {
  try {
    const r = await api(`/api/companies/${encodeURIComponent(c.symbol)}/quote`);
    if (r.quote && current.company === c) { c.quote = r.quote; fill($("#co-px"), priceBlock(c)); }
  } catch { /* the header keeps the stored price */ }
}

// Fetch filings, prices and rates now, then redraw the page. Shows progress in place of the button's label.
async function runRefresh(c, tab, btn, note) {
  btn.disabled = true; btn.textContent = "Refreshing…";
  const started = Date.now();
  const tick = note ? setInterval(() => { note.textContent = `${Math.round((Date.now() - started) / 1000)}s`; }, 1000) : null;
  try {
    const r = await api(`/api/companies/${encodeURIComponent(c.symbol)}/refresh`, { method: "POST", timeout: 180000 });
    const errs = Object.entries(r.errors || {});
    if (current.symbol !== c.symbol) return;  // the user moved on
    current.company = null;
    STATUS = null; await loadStatus();  // rates may be new
    await companyView(c.symbol, currentTab());
    if (errs.length) view.prepend(h("div", { class: "callout bad", style: "margin-bottom:12px" }, h("strong", {}, "Some sources failed. "), errs.map(([k, v]) => `${k}: ${v}`).join(" · ")));
  } catch (e) {
    btn.disabled = false; btn.textContent = "Refresh from sources";
    if (current.symbol === c.symbol) $("#auto-load")?.replaceWith(h("div", { class: "callout bad", style: "margin-bottom:12px" }, `Loading from the sources failed: ${e.message}`));
  } finally { if (tick) clearInterval(tick); }
}
function currentTab() {
  const t = location.hash.replace(/^#\/?/, "").split("/")[2];
  return TABS.some(([k]) => k === t) ? t : "overview";
}
// A company opened for the first time loads its SEC filings, prices and rates by itself, once per visit.
const autoLoaded = new Set();
function needsAutoLoad(c) {
  if (!STATUS?.live || autoLoaded.has(c.symbol)) return false;
  const noFilings = (STATUS.secEnabled && c.listing?.cik != null && !c.sec) || (c.listing?.coverage === "prices-only" && !c.sec && !c.filer);
  const noRates = !STATUS.rates?.DGS10 || !STATUS.rates?.BAA10Y;
  return noFilings || noRates || !c.loaded;
}
function autoLoad(c, tab, btn) {
  autoLoaded.add(c.symbol);
  const note = h("span", { class: "faint" });
  const what = STATUS.secEnabled && c.listing?.cik != null && !c.sec ? "SEC filings, prices and interest rates" : "prices and interest rates";
  view.insertBefore(h("div", { id: "auto-load", class: "callout", style: "margin-bottom:12px" },
    h("strong", {}, `Loading ${what} for ${c.symbol}… `), "First visit only; usually 10 to 40 seconds. The page updates by itself. ", note),
    view.children[2] || null);
  runRefresh(c, tab, btn, note);
}

function tabsBar(c, tab) {
  const btn = h("button", { class: "btn primary", id: "refresh-btn", title: "Fetch filings, prices and rates from the live sources now",
    onclick: () => runRefresh(c, tab, btn) }, "Refresh from sources");
  return h("nav", { class: "tabs" },
    TABS.map(([k, label]) => h("a", { href: `#/c/${encodeURIComponent(c.symbol)}/${k}`, "aria-current": k === tab ? "page" : null }, label)),
    h("span", { class: "spacer" }),
    h("a", { class: "btn", style: "line-height:26px;text-decoration:none;margin-right:8px", href: `#/portfolio/trade/${encodeURIComponent(c.symbol)}`, title: "Open the paper portfolio with a trade ticket for this company" }, "Paper trade"),
    btn);
}

// --- overview ------------------------------------------------------------------------------------------------
async function overviewTab(c, body) {
  const cur = c.listing?.currency || "USD";
  const left = h("div", { class: "stack" });
  const right = h("div", { class: "stack" });
  fill(body, h("div", { class: "grid cols-2" }, left, right));

  if (!c.loaded) {
    left.append(h("div", { class: "callout warn" }, h("strong", {}, "Nothing stored for this listing yet. "),
      STATUS?.live ? "Loading it from the sources now." : "Live sources are off, so there is nothing to load (KEERS_OFFLINE)."));
  }
  if (c.listing?.coverage === "prices-only" && !c.sec && !c.filer) {
    left.append(h("div", { class: "callout" }, c.listing.coverageText));
  }
  if (c.snapshot) {
    const sn = c.snapshot;
    left.append(sn.blocked
      ? h("div", { class: "callout bad" }, h("strong", {}, `Snapshot as of ${sn.asOf} is blocked, so the valuation will not run.`), h("ul", {}, sn.blockedBy.map((b) => h("li", {}, b))))
      : h("div", { class: "callout" }, `Snapshot as of ${sn.asOf}: complete and fresh enough to value. Figures are labelled `, h("strong", {}, "filed"), ", not verified, until a cross-check exists."));
    const groups = [...new Set(c.figures.map((f) => f.group))];
    const rows = [];
    for (const g of groups) {
      rows.push(h("tr", { class: "group" }, h("td", { colspan: 5 }, g)));
      for (const f of c.figures.filter((x) => x.group === g)) {
        const tr = h("tr", { class: "clickable", onclick: () => { closeDrawer(); tr.classList.add("sel"); openDrawer(f.title, f, cur); } },
          h("td", {}, f.title),
          h("td", { class: `n ${isNum(f.value) && f.value < 0 ? "neg" : ""}` }, fval(f.value, f.unit)),
          h("td", { class: "muted" }, unitText(f.unit, cur)),
          h("td", {}, chips(f), (f.warnings || []).length ? h("span", { class: "faint", title: f.warnings.join("\n") }, " ⚠") : null),
          h("td", { class: "n mono muted" }, fdate(f.periodEnd)));
        rows.push(tr);
      }
    }
    left.append(h("section", { class: "panel" },
      h("div", { class: "ph" }, h("h2", {}, "Key figures"), h("span", { class: "faint" }, "Click a row to see where it came from")),
      h("div", { class: "pb flush scroll-x" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Figure"), h("th", { class: "n" }, "Value"), h("th", {}, "Unit"), h("th", {}, "Label"), h("th", { class: "n" }, "Period end"))),
        h("tbody", {}, rows))),
    ));
    left.append(h("section", { class: "panel" },
      h("div", { class: "ph" }, h("h2", {}, "Multiples")),
      h("div", { class: "pb flush" }, h("table", {}, h("tbody", {}, c.ratios.map((r) => h("tr", { class: "clickable", onclick: () => openDrawer(r.title, r, cur) },
        h("td", {}, r.title), h("td", { class: "n" }, fval(r.value, r.unit)), h("td", {}, chips(r)), h("td", { class: "muted" }, r.method)))))),
    ));
    if (sn.warnings.length) {
      left.append(h("details", { class: "panel pb" }, h("summary", {}, `Data warnings (${sn.warnings.length})`), h("ul", {}, sn.warnings.map((w) => h("li", {}, w)))));
    }
  }
  // right column: chart, filings, headlines
  const chartBox = h("div", { class: "chart small" });
  right.append(h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Price, daily"), h("a", { href: `#/c/${encodeURIComponent(c.symbol)}/chart` }, "Full chart")),
    h("div", { class: "pb" }, chartBox, h("div", { class: "faint", id: "mini-meta", style: "margin-top:6px" }))));
  if (c.filings.length) {
    right.append(h("section", { class: "panel" },
      h("div", { class: "ph" }, h("h2", {}, "Recent periodic filings"), h("span", { class: "faint" }, c.filer ? "ESEF, via filings.xbrl.org" : "SEC EDGAR")),
      h("div", { class: "pb flush" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Form"), h("th", {}, "Period"), h("th", { class: "n" }, "Filed"), h("th", {}, ""))),
        h("tbody", {}, c.filings.map((f) => h("tr", {}, h("td", { class: "mono" }, f.form), h("td", { class: "mono" }, fdate(f.period)),
          h("td", { class: "n mono" }, f.filed), h("td", { class: "n" }, extLink(f.link, "Filing")))))))));
  }
  const newsBox = h("div", { class: "pb flush" }, h("p", { class: "muted", style: "padding:12px" }, "Loading headlines…"));
  right.append(h("section", { class: "panel" }, h("div", { class: "ph" }, h("h2", {}, "Latest headlines"), h("a", { href: `#/c/${encodeURIComponent(c.symbol)}/news` }, "All")), newsBox));

  drawPriceChart(chartBox, c.symbol, "1d", { small: true, meta: $("#mini-meta") });
  api(`/api/companies/${encodeURIComponent(c.symbol)}/news`).then((n) => fill(newsBox, newsList(n.items.slice(0, 5), n))).catch((e) => fill(newsBox, h("p", { class: "err", style: "padding:12px" }, e.message)));
}

// --- chart ---------------------------------------------------------------------------------------------------
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
async function drawPriceChart(box, symbol, interval, { small = false, meta = null } = {}) {
  fill(box, h("div", { class: "empty" }, "Loading bars…"));
  let r;
  try { r = await api(`/api/companies/${encodeURIComponent(symbol)}/bars?interval=${interval}`); }
  catch (e) { fill(box, h("div", { class: "empty err" }, e.message)); return null; }
  if (meta) {
    fill(meta, 
      r.source ? `${r.bars.length} bars from ${r.source}` : "No bars stored",
      r.last ? ` · last ${interval === "1d" ? fdate(r.last) : fdatetime(r.last)}` : "",
      ` · unadjusted · ${r.timezone} `, chip("market"), r.stale ? chip("stale") : null,
      r.error ? h("div", { class: "err" }, `Live fetch failed, showing stored bars: ${r.error}`) : null,
      (r.notes || []).length ? h("div", {}, r.notes.join(" · ")) : null,
    );
  }
  if (!r.bars.length) { fill(box, h("div", { class: "empty" }, r.error ? `No bars: ${r.error}` : "No bars stored for this interval yet.")); return null; }
  fill(box, );
  const LW = window.LightweightCharts;
  const chart = LW.createChart(box, {
    autoSize: true,
    localization: { locale: "en-GB" },
    layout: { background: { type: "solid", color: cssVar("--panel") }, textColor: cssVar("--muted"), fontFamily: cssVar("--mono"), fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: cssVar("--line") }, horzLines: { color: cssVar("--line") } },
    rightPriceScale: { borderColor: cssVar("--line-strong") },
    timeScale: { borderColor: cssVar("--line-strong"), timeVisible: interval !== "1d", secondsVisible: false },
    crosshair: { mode: 0 },
  });
  const up = cssVar("--up"), down = cssVar("--down");
  if (small) {
    const s = chart.addSeries(LW.LineSeries, { color: cssVar("--accent"), lineWidth: 1.5, priceLineVisible: false });
    s.setData(r.bars.map((b) => ({ time: b.time, value: b.close })));
  } else {
    const s = chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
    s.setData(r.bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    const vol = chart.addSeries(LW.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
    vol.setData(r.bars.filter((b) => isNum(b.volume)).map((b) => ({ time: b.time, value: b.volume, color: (b.close >= b.open ? up : down) + "66" })));
  }
  chart.timeScale().fitContent();
  return chart;
}

async function chartTab(c, body) {
  const box = h("div", { class: "chart" });
  const meta = h("div", { class: "faint", style: "margin-top:8px" });
  const intervals = [["1d", "1D"], ["60m", "1H"], ["15m", "15m"], ["5m", "5m"]];
  let active = "1d";
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Bar interval" }, intervals.map(([k, label]) => h("button", { "aria-pressed": k === active ? "true" : "false", onclick: async (e) => {
    active = k; seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.target.setAttribute("aria-pressed", "true");
    if (chartHandle) chartHandle.remove();
    chartHandle = await drawPriceChart(box, c.symbol, active, { meta });
  } }, label)));
  fill(body, h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, `${c.symbol} price`), seg),
    h("div", { class: "pb" }, box, meta,
      h("p", { class: "faint", style: "margin-top:8px" }, "Bars are drawn from Keers & Co's own store. A bar still forming is never shown as complete. Daily bars are unadjusted closes from one source; the beta and drawdown use adjusted closes."))));
  chartHandle = await drawPriceChart(box, c.symbol, active, { meta });
}

// --- news ----------------------------------------------------------------------------------------------------
function newsList(items, n) {
  if (!items.length) return h("p", { class: "muted", style: "padding:12px" }, n.error ? `No headlines: ${n.error}` : "No headlines stored.");
  return h("ul", { class: "news" }, items.map((i) => h("li", {},
    h("span", { class: "when" }, i.published ? fdatetime(i.published) : "undated"),
    h("div", {}, extLink(i.link, i.title), h("div", { class: "pub" }, i.publisher, i.related?.length ? ` · ${i.related.join(", ")}` : "")),
  )));
}
async function newsTab(c, body) {
  fill(body, h("div", { class: "loading" }, "Loading headlines…"));
  const n = await api(`/api/companies/${encodeURIComponent(c.symbol)}/news`);
  fill(body, h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Headlines"), h("span", { class: "faint" }, "Yahoo Finance · untrusted third-party text")),
    h("div", { class: "pb" }, h("div", { class: "callout" }, n.note), n.error ? h("p", { class: "err", style: "margin-top:8px" }, `Live fetch failed, showing stored headlines: ${n.error}`) : null),
    h("div", { class: "pb flush" }, newsList(n.items, n))));
}

// --- research memo (keers.agents) ----------------------------------------------------------------------------
// The memo is shown as text (never as HTML), so nothing a model writes can run in the page.
function memoInline(text) {
  // [label](https://...) becomes a link; **, * and ` marks are dropped. Everything else stays plain text.
  const out = [];
  let last = 0;
  const plain = (t) => t.replace(/\*\*|`/g, "").replace(/(^|\s)\*(\S[^*]*)\*/g, "$1$2");
  for (const m of text.matchAll(/\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g)) {
    out.push(plain(text.slice(last, m.index)), extLink(m[2], plain(m[1])));
    last = m.index + m[0].length;
  }
  out.push(plain(text.slice(last)));
  return out;
}
function memoBlock(md) {
  const out = [];
  let list = null, table = null;
  for (const line of String(md).split("\n")) {
    if (!line.startsWith("|")) table = null;
    const m = line.match(/^(#{1,3}) (.*)$/);
    if (m) { list = null; out.push(h(m[1].length === 1 ? "h2" : "h3", { style: "margin:14px 0 6px" }, memoInline(m[2]))); continue; }
    if (line.startsWith("|")) {
      if (/^\|[-| ]+\|$/.test(line)) continue;
      const cells = line.slice(1, -1).split("|").map((x) => x.trim());
      if (!table) { table = h("table", { style: "margin:6px 0" }); out.push(table); table.append(h("tr", {}, cells.map((x) => h("th", {}, x)))); }
      else table.append(h("tr", {}, cells.map((x) => h("td", {}, memoInline(x)))));
      continue;
    }
    if (/^(- |\d+\. )/.test(line)) {
      if (!list) { list = h(/^\d/.test(line) ? "ol" : "ul", { style: "margin:4px 0;padding-left:20px" }); out.push(list); }
      list.append(h("li", {}, memoInline(line.replace(/^(- |\d+\. )/, ""))));
      continue;
    }
    list = null;
    if (line.trim()) out.push(h("p", { style: "margin:6px 0" }, memoInline(line.replace(/^> /, ""))));
  }
  return out;
}
async function researchTab(c, body) {
  const list = await api("/api/portfolios").catch(() => ({ portfolios: [] }));
  const strat = h("select", { "aria-label": "Strategy" },
    [["B", "B · Durable-moat compounders"], ["A", "A · Distressed and special situations"], ["C", "C · Systematic quant"]].map(([v, t]) => h("option", { value: v }, t)));
  const port = h("select", { "aria-label": "Paper portfolio" }, h("option", { value: "" }, "No portfolio (memo only)"),
    list.portfolios.map((p) => h("option", { value: p.id }, p.name)));
  const out = h("div", {});
  const go = async (execute) => {
    if (execute && !port.value) { fill(out, h("p", { class: "err" }, "Choose a paper portfolio to place the trade in.")); return; }
    fill(out, h("div", { class: "loading" }, "The analyst, bear reviewer and memo writer are working… (up to a few minutes with a model)"));
    try {
      const r = await api(`/api/companies/${encodeURIComponent(c.symbol)}/research`, { method: "POST", timeout: 600000,
        body: JSON.stringify({ strategy: strat.value, portfolio: port.value || null, execute }) });
      const t = r.trade;
      fill(out,
        h("div", { class: `callout ${r.decision.propose ? "" : "warn"}`, style: "margin-bottom:10px" },
          h("strong", {}, r.decision.propose ? "Trade proposed. " : "No trade. "),
          t ? (t.error ? `Paper desk: ${t.error}` : t.executed ? (t.filled ? "Placed and filled on paper." : "Placed, but the risk limits rejected it.") : (t.risk?.approved ? "Preview: it would pass the risk limits." : "Preview: the risk limits would block it.")) : "",
          h("div", { class: "faint", style: "margin-top:4px" }, `Written by: ${r.llm} · saved as agent-runs/${r.id}.md`)),
        h("section", { class: "panel" }, h("div", { class: "pb" }, memoBlock(r.memo))));
    } catch (e) { fill(out, h("p", { class: "err" }, e.message)); }
  };
  fill(body,
    h("section", { class: "panel", style: "margin-bottom:12px" },
      h("div", { class: "ph" }, h("h2", {}, "Research memo"), h("span", { class: "faint" }, "analyst · bear reviewer · IC memo writer")),
      h("div", { class: "pb" },
        h("div", { class: "callout", style: "margin-bottom:10px" }, "The agents write words; every figure comes from the engines and is numbered to its source. ",
          "Without ANTHROPIC_API_KEY they follow stated rules (rules-v1). A proposed trade is sized by code and must pass the risk limits; paper only."),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" }, strat, port,
          h("button", { class: "btn primary", onclick: () => go(false) }, "Write memo"),
          h("button", { class: "btn", onclick: () => go(true), title: "Writes the memo and places the proposed trade on paper, if the risk limits allow it" }, "Write memo and place paper trade")))),
    out);
}

// --- distress ------------------------------------------------------------------------------------------------
const CHECK_NAMES = { interestCover: "Interest cover", netLeverage: "Net debt / EBITDA", debtDue: "Debt due within a year", altmanZ: "Altman Z", drawdown: "Fall from 52-week high" };
function checkValue(c) {
  if (!isNum(c.value)) return "—";
  if (c.name === "debtDue" || c.name === "drawdown") return fpct(c.value, 0);
  if (c.name === "altmanZ") return fnum(c.value, 2);
  return fx(c.value, 1);
}
async function distressTab(c, body) {
  fill(body, h("div", { class: "loading" }, "Running the screen…"));
  const r = await api(`/api/companies/${encodeURIComponent(c.symbol)}/distress`);
  const cr = r.credit || {};
  const head = h("div", { class: "kpis" },
    h("div", { class: "kpi" }, h("div", { class: "eyebrow" }, "Overall"), h("div", { class: "v" }, levelBadge(r.status)), h("div", { class: "s" }, `${r.screenVersion}${r.periodEnd ? ` · accounts to ${r.periodEnd}` : ""}`)),
    h("div", { class: "kpi" }, h("div", { class: "eyebrow" }, "Credit backdrop"), h("div", { class: "v" }, levelBadge(cr.level || "unknown")), h("div", { class: "s" }, cr.reason || "")),
    h("div", { class: "kpi" }, h("div", { class: "eyebrow" }, "Baa spread"), h("div", { class: "v" }, isNum(cr.spread) ? `${fnum(cr.spread, 2)}pp` : "—"), h("div", { class: "s" }, cr.day ? `FRED BAA10Y on ${cr.day}${isNum(cr.change) ? ` · ${signed(fnum(cr.change, 2), cr.change)}pp in 3 months` : ""}` : "")),
  );
  const parts = [head];
  if (r.status === "blocked" || r.status === "not available") {
    parts.push(h("div", { class: "callout bad", style: "margin-top:16px" }, h("strong", {}, "The screen did not run. "), h("ul", {}, (r.blockedBy || []).map((b) => h("li", {}, b)))));
  } else {
    const rows = [];
    for (const ch of r.checks) {
      const inputs = Object.entries(ch.inputs || {});
      const detail = h("tr", { hidden: true }, h("td", { colspan: 5 }, inputs.length ? h("table", {}, h("tbody", {}, inputs.map(([k, v]) => {
        if (v && typeof v === "object" && "label" in v) {
          return h("tr", { class: "clickable", onclick: () => openDrawer(k, v, "USD") }, h("td", {}, k), h("td", { class: "n" }, fval(v.value, v.unit)), h("td", {}, chips(v)), h("td", { class: "muted" }, v.method));
        }
        return h("tr", {}, h("td", {}, k), h("td", { class: "n mono" }, isNum(v) ? fnum(v, 2) : String(v ?? "—")), h("td", {}, chip("market")), h("td", {}));
      }))) : h("span", { class: "muted" }, "No inputs recorded.")));
      rows.push(h("tr", { class: "clickable", onclick: () => (detail.hidden = !detail.hidden) },
        h("td", {}, CHECK_NAMES[ch.name] || ch.name), h("td", {}, levelBadge(ch.level)), h("td", { class: "n" }, checkValue(ch)),
        h("td", {}, ch.reason), h("td", { class: "muted" }, ch.thresholds)), detail);
    }
    parts.push(h("section", { class: "panel", style: "margin-top:16px" },
      h("div", { class: "ph" }, h("h2", {}, "Checks"), h("span", { class: "faint" }, "Click a check for its inputs and their sources. Unknown never counts as clear.")),
      h("div", { class: "pb flush scroll-x" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Check"), h("th", {}, "Level"), h("th", { class: "n" }, "Value"), h("th", {}, "Why"), h("th", {}, "Thresholds"))),
        h("tbody", {}, rows)))));
    if ((r.dataWarnings || []).length) parts.push(h("div", { class: "callout warn", style: "margin-top:16px" }, h("strong", {}, "Snapshot issues behind these checks"), h("ul", {}, r.dataWarnings.map((w) => h("li", {}, w)))));
  }
  parts.push(h("p", { class: "faint", style: "margin-top:12px" }, "A screen for research, not a verdict: it lists companies worth a closer look. Rules: two severe checks make a company distressed; one severe or two watch flags, stressed; any unknown check without a severe flag, incomplete."));
  fill(body, ...parts);
}

// --- valuation -----------------------------------------------------------------------------------------------
const DCF_FIELDS = [
  ["beta", "Beta", "x"], ["riskFree", "Risk-free rate", "pct"], ["erp", "Equity risk premium", "pct"], ["sizePremium", "Size premium", "pct"],
  ["preTaxCostOfDebt", "Pre-tax cost of debt", "pct"], ["debtWeight", "Debt weight", "pct"], ["taxRate", "Tax rate", "pct"],
  ["terminalGrowth", "Terminal growth", "pct"], ["exitMultiple", "Exit multiple (EV/EBITDA)", "x"], ["daPct", "D&A, % of revenue", "pct"], ["nwcPct", "Working capital, % of revenue change", "pct"],
  ["revenueGrowth", "Revenue growth by year", "pct"], ["ebitdaMargin", "EBITDA margin by year", "pct"], ["capexPct", "Capex, % of revenue by year", "pct"],
];
const LBO_FIELDS = [
  ["entryEVOverride", "Entry EV (with premium)", "USDm"], ["exitMultiple", "Exit multiple (EV/EBITDA)", "x"],
  ["tranches.0.leverage", "Senior debt, x EBITDA", "x"], ["tranches.0.rate", "Senior rate", "pct"],
  ["tranches.1.leverage", "Subordinated debt, x EBITDA", "x"], ["tranches.1.rate", "Subordinated rate", "pct"],
  ["transactionFeePct", "Transaction fees", "pct"], ["financingFeePct", "Financing fees", "pct"], ["cashSweepPct", "Cash sweep", "pct"],
  ["taxRate", "Tax rate", "pct"], ["daPct", "D&A, % of revenue", "pct"], ["nwcPct", "Working capital, % of revenue change", "pct"], ["exitFeePct", "Exit fees", "pct"],
  ["revenueGrowth", "Revenue growth by year", "pct"], ["ebitdaMargin", "EBITDA margin by year", "pct"], ["capexPct", "Capex, % of revenue by year", "pct"],
];
function pathGet(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[/^\d+$/.test(k) ? Number(k) : k]), obj); }
// Inputs are shown in display units (percent as 4.5, not 0.045); converting back is a unit change, not a calculation.
const toShow = (v, unit) => (isNum(v) ? (unit === "pct" ? +(v * 100).toFixed(4) : +v.toFixed(4)) : "");
const fromShow = (s, unit) => { const n = Number(String(s).trim()); return String(s).trim() === "" || !Number.isFinite(n) ? NaN : unit === "pct" ? n / 100 : n; };

let EDITS = { lbo: {}, dcf: {} };
function inputsTable(kind, fields, model, defaults) {
  const rows = fields.map(([path, title, unit]) => {
    const now = pathGet(model, path), def = pathGet(defaults, path);
    const edited = Object.prototype.hasOwnProperty.call(EDITS[kind], path);
    const mk = (v, d, i) => {
      const inp = h("input", { inputmode: "decimal", value: toShow(v, unit), "aria-label": `${title}${i !== null ? ` year ${i + 1}` : ""}`, class: edited ? "changed" : "" });
      inp.dataset.def = toShow(d, unit);
      inp.addEventListener("change", () => {
        const vals = Array.isArray(now) ? [...inp.closest(".yrs").querySelectorAll("input")].map((x) => fromShow(x.value, unit)) : fromShow(inp.value, unit);
        const bad = Array.isArray(vals) ? vals.some((x) => Number.isNaN(x)) : Number.isNaN(vals);
        inp.classList.toggle("bad", bad);
        if (bad) return;
        EDITS[kind][path] = vals;
        inp.classList.add("changed");
      });
      return inp;
    };
    const cell = Array.isArray(now) ? h("div", { class: "yrs" }, now.map((v, i) => mk(v, Array.isArray(def) ? def[i] : def, i))) : mk(now, def, null);
    const defText = Array.isArray(def) ? def.map((d) => (unit === "pct" ? fpct(d, 1) : fnum(d, 2))).join(" / ") : unit === "pct" ? fpct(def, 2) : unit === "USDm" ? fnum(def, 0) : fnum(def, 2);
    return h("tr", {}, h("td", {}, title), h("td", { class: "n" }, cell), h("td", { class: "muted" }, unit === "pct" ? "%" : unit === "USDm" ? "USD m" : "x"),
      h("td", {}, edited ? h("span", { class: "chip custom" }, "your input") : chip("assumption")), h("td", { class: "n mono muted" }, defText));
  });
  return h("table", { class: "inputs" }, h("thead", {}, h("tr", {}, h("th", {}, "Input"), h("th", { class: "n" }, "Value"), h("th", {}, "Unit"), h("th", {}, "Label"), h("th", { class: "n" }, "kci-v3 default"))), h("tbody", {}, rows));
}

function kpi(title, value, sub, ...labels) {
  return h("div", { class: "kpi" }, h("div", { class: "eyebrow" }, title, " ", labels), h("div", { class: "v" }, value), h("div", { class: "s" }, sub));
}
function cmpVal(v, unit) { if (unit === "text") return v ?? "—"; if (unit === "pct") return fpct(v, 1); if (unit === "x") return fx(v, 2); if (unit === "score") return fnum(v, 0); return fnum(v, 0); }

async function valuationTab(c, body, edits = null) {
  fill(body, h("div", { class: "loading" }, "Running kci-v3 and kci-v1…"));
  if (!edits) EDITS = { lbo: {}, dcf: {} };
  const path = `/api/companies/${encodeURIComponent(c.symbol)}/valuation`;
  const v = edits ? await api(path, { method: "POST", body: JSON.stringify(edits) }) : await api(path);
  if (!v.ran) {
    fill(body, h("div", { class: "callout bad" }, h("strong", {}, `The ${v.calcVersion} valuation did not run. `), "A blocked snapshot never reaches the engine; a missing check is never an approval.", h("ul", {}, (v.blockedBy || []).map((b) => h("li", {}, b)))));
    return;
  }
  const a = v.v2, dcf = a.dcf, lbo = a.lbo, base = a.scenarios.base.result;
  const custom = Object.keys(v.custom || {}).length > 0;
  const rerun = async () => {
    const payload = { lbo: EDITS.lbo, dcf: EDITS.dcf };
    try { await valuationTab(c, body, payload); } catch (e) { alert(e.message); }
  };
  const reset = () => valuationTab(c, body);

  const kpis = h("div", { class: "kpis" },
    kpi("IC screen decision", a.rec.decision, a.rec.rationale, chip("estimate")),
    kpi("Base case IRR", base.ok ? fpct(base.returns.irr, 1) : "—", base.ok ? `MOIC ${fx(base.returns.moic)} over ${a.lboIn.holdYears} years` : (base.errors || []).join("; "), chip("estimate")),
    kpi("DCF enterprise value", dcf.ok ? fnum(dcf.primary.ev, 0) : "—", dcf.ok ? `USD m · ${dcf.method === "both" ? "Gordon growth" : dcf.method} terminal · TV ${fpct(dcf.primary.tvShareOfEV, 0)} of EV` : (dcf.errors || []).join("; "), chip("estimate")),
    kpi("DCF value per share", fnum(v.perShare.value, 2), v.perShare.vsPrice ? `${signed(fpct(v.perShare.vsPrice.value, 1), v.perShare.vsPrice.value)} vs last close` : (v.perShare.warnings || []).join("; "), chip("estimate")),
    kpi("WACC", a.wacc.ok ? fpct(a.wacc.value, 2) : "—", a.wacc.ok ? `cost of equity ${fpct(a.wacc.costOfEquity, 2)} · debt ${fpct(a.wacc.afterTaxCostOfDebt, 2)} after tax` : (a.wacc.errors || []).join("; "), chip("estimate")),
  );

  const status = h("div", { class: custom ? "callout warn" : "callout", style: "margin:12px 0" },
    h("strong", {}, `${v.calcVersion} on live data as of ${v.asOf}`), ` (engine ${v.engineVersion}, data status: ${v.dataStatus}). `,
    custom ? ["Running with your inputs: ", Object.entries(v.custom).map(([k, ks]) => `${k.toUpperCase()} ${ks.join(", ")}`).join("; "), ". ", h("button", { class: "btn", onclick: reset }, "Back to defaults")] : "Default assumptions. Edit any input below and re-run; nothing is saved.");

  // comparison
  const cmp = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "kci-v1 against " + v.calcVersion + ", same live inputs"), h("span", { class: "faint" }, custom ? v.calcVersion + " column uses your inputs" : "")),
    h("div", { class: "pb flush scroll-x" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Output"), h("th", { class: "n" }, "kci-v1"), h("th", { class: "n" }, v.calcVersion), h("th", { class: "n" }, "Change"))),
      h("tbody", {}, v.comparison.map((r) => h("tr", {}, h("td", {}, r.title), h("td", { class: "n" }, cmpVal(r.v1, r.unit)), h("td", { class: "n" }, cmpVal(r.v2, r.unit)),
        h("td", { class: `n ${isNum(r.change) ? (r.change < 0 ? "neg" : r.change > 0 ? "pos" : "") : ""}` }, isNum(r.change) ? signed(r.unit === "pct" ? `${fnum(r.change * 100, 1)}pp` : cmpVal(r.change, r.unit), r.change) : "")))))),
    h("div", { class: "pb" }, h("div", { class: "eyebrow" }, "What " + v.calcVersion + " changes"), h("ul", {}, v.changes.map((x) => h("li", {}, x)))));

  // DCF
  const dcfRows = dcf.ok ? dcf.rows : [];
  const dcfPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Discounted cash flow"), h("span", {}, chip("estimate"))),
    dcf.ok ? h("div", { class: "pb flush scroll-x" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "USD m"), dcfRows.map((r) => h("th", { class: "n" }, `Y${r.year}`)))),
      h("tbody", {}, [["Revenue", "revenue"], ["Growth", "growth", "pct"], ["EBITDA", "ebitda"], ["Margin", "margin", "pct"], ["D&A", "da"], ["EBIT", "ebit"], ["Tax", "tax"], ["NOPAT", "nopat"], ["Capex", "capex"], ["Change in working capital", "dNwc"], ["Unlevered free cash flow", "ufcf"], ["Discount factor", "discountFactor", "df"], ["Present value", "pv"]]
        .map(([t, k, u]) => h("tr", {}, h("td", {}, t), dcfRows.map((r) => h("td", { class: `n ${isNum(r[k]) && r[k] < 0 ? "neg" : ""}` }, u === "pct" ? fpct(r[k], 1) : u === "df" ? fnum(r[k], 3) : fnum(r[k], 0)))))),
    )) : h("div", { class: "pb" }, h("div", { class: "callout bad" }, "DCF did not run: ", (dcf.errors || []).join("; "))),
    dcf.ok ? h("div", { class: "pb" }, h("table", {}, h("tbody", {},
      h("tr", {}, h("td", {}, "Sum of PV of cash flows"), h("td", { class: "n" }, fnum(dcf.sumPV, 0))),
      h("tr", {}, h("td", {}, "PV of terminal value (Gordon growth)"), h("td", { class: "n" }, fnum(dcf.terminal.gordon?.pvTV, 0)), h("td", { class: "muted" }, `implies ${fx(dcf.terminal.gordon?.impliedExitMultiple, 1)} EBITDA`)),
      dcf.terminal.exit ? h("tr", {}, h("td", {}, "Cross-check: exit-multiple terminal EV"), h("td", { class: "n" }, fnum(dcf.terminal.exit.ev, 0)), h("td", { class: "muted" }, `implies ${fpct(dcf.terminal.exit.impliedPerpetualGrowth, 1)} perpetual growth`)) : null,
      h("tr", {}, h("td", {}, "Enterprise value"), h("td", { class: "n" }, h("strong", {}, fnum(dcf.primary.ev, 0)))),
      h("tr", {}, h("td", {}, "Less net debt"), h("td", { class: "n" }, fnum(dcf.claims?.netDebt, 0))),
      dcf.claims?.otherClaims ? h("tr", {}, h("td", {}, "Less other claims"), h("td", { class: "n" }, fnum(dcf.claims.otherClaims, 0))) : null,
      dcf.claims?.nonOperatingAssets ? h("tr", {}, h("td", {}, "Plus non-operating assets"), h("td", { class: "n" }, fnum(dcf.claims.nonOperatingAssets, 0))) : null,
      h("tr", {}, h("td", {}, "Equity value"), h("td", { class: "n" }, h("strong", {}, fnum(dcf.primary.equity, 0)))),
    )), (dcf.warnings || []).length ? h("div", { class: "callout warn" }, dcf.warnings.join(" · ")) : null) : null,
    h("div", { class: "pb" }, h("details", { open: Object.keys(EDITS.dcf).length > 0 }, h("summary", {}, "DCF inputs (editable)"), inputsTable("dcf", DCF_FIELDS, a.dcfIn, v.defaults.dcf),
      h("div", { style: "margin-top:8px;display:flex;gap:8px" }, h("button", { class: "btn primary", onclick: rerun }, "Re-run with these inputs"), h("button", { class: "btn", onclick: reset }, "Reset")))),
  );

  // LBO
  const yrs = lbo.ok ? lbo.years : [];
  const lboPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Leveraged buyout"), h("span", {}, chip("estimate"))),
    lbo.ok ? h("div", { class: "pb grid cols-2" },
      h("div", {}, h("div", { class: "eyebrow" }, "Sources"), h("table", {}, h("tbody", {}, lbo.sources.map((s) => h("tr", {}, h("td", {}, s.label), h("td", { class: "n" }, fnum(s.value, 0)), h("td", { class: "n muted" }, isNum(s.multiple) ? fx(s.multiple, 2) : ""))),
        h("tr", {}, h("td", {}, h("strong", {}, "Total")), h("td", { class: "n" }, h("strong", {}, fnum(lbo.totalSources, 0))), h("td", {}))))),
      h("div", {}, h("div", { class: "eyebrow" }, "Uses"), h("table", {}, h("tbody", {}, lbo.uses.map((s) => h("tr", {}, h("td", {}, s.label), h("td", { class: "n" }, fnum(s.value, 0)))),
        h("tr", {}, h("td", {}, h("strong", {}, "Total")), h("td", { class: "n" }, h("strong", {}, fnum(lbo.totalUses, 0))))))),
    ) : h("div", { class: "pb" }, h("div", { class: "callout bad" }, "LBO did not run: ", (lbo.errors || []).join("; "))),
    a.lboIn.takeover?.entryEV ? h("div", { class: "pb muted" }, `Entry at market EV plus a takeover premium of ${fnum(a.lboIn.takeover.premium, 0)}m: ${fx(a.lboIn.takeover.entryMultiple, 2)} EBITDA against a market multiple of ${fx(a.lboIn.takeover.marketMultiple, 2)}, which is also the exit multiple.`) : null,
    lbo.ok ? h("div", { class: "pb flush scroll-x" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "USD m"), yrs.map((y) => h("th", { class: "n" }, `Y${y.year}`)))),
      h("tbody", {}, [["Revenue", "revenue"], ["EBITDA", "ebitda"], ["Interest expense", "interestExpense"], ["Net income", "netIncome"], ["Free cash flow", "fcf"], ["Debt repaid", "debtRepaid"], ["Total debt", "totalDebt"], ["Net debt / EBITDA", "netLeverage", "x"], ["Interest cover", "interestCoverage", "x"]]
        .map(([t, k, u]) => h("tr", {}, h("td", {}, t), yrs.map((y) => h("td", { class: `n ${isNum(y[k]) && y[k] < 0 ? "neg" : ""}` }, u === "x" ? fx(y[k], 2) : fnum(y[k], 0)))))))) : null,
    h("div", { class: "pb" }, h("div", { class: "eyebrow" }, "Scenarios"), h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Scenario"), h("th", { class: "n" }, "Growth"), h("th", { class: "n" }, "Exit margin"), h("th", { class: "n" }, "Exit multiple"), h("th", { class: "n" }, "Rate shift"), h("th", { class: "n" }, "IRR"), h("th", { class: "n" }, "MOIC"))),
      h("tbody", {}, ["downside", "base", "upside"].map((k) => { const s = a.scenarios[k], i = s.inputs, res = s.result; return h("tr", {}, h("td", {}, k),
        h("td", { class: "n" }, fpct(i.revenueGrowth, 1)), h("td", { class: "n" }, fpct(i.exitMargin, 1)), h("td", { class: "n" }, fx(i.exitMultiple, 2)), h("td", { class: "n" }, fpct(i.rateShift, 1)),
        h("td", { class: "n" }, res.ok ? fpct(res.returns.irr, 1) : "—"), h("td", { class: "n" }, res.ok ? fx(res.returns.moic, 2) : (res.errors || []).join("; "))); })))),
    lbo.ok ? h("div", { class: "pb" }, h("details", {}, h("summary", {}, `Model checks: ${lbo.allChecksPass ? "all reconcile" : "SOME FAIL"} (${lbo.checks.length})`),
      h("table", {}, h("tbody", {}, lbo.checks.map((x) => h("tr", {}, h("td", { class: "mono" }, `Y${x.year}`), h("td", {}, x.check), h("td", { class: x.ok ? "pos" : "neg" }, x.ok ? "ok" : "fails"), h("td", { class: "n mono muted" }, fnum(x.diff, 6)))))))) : null,
    h("div", { class: "pb" }, h("details", { open: Object.keys(EDITS.lbo).length > 0 }, h("summary", {}, "LBO inputs (editable)"), inputsTable("lbo", LBO_FIELDS, a.lboIn, v.defaults.lbo),
      h("div", { style: "margin-top:8px;display:flex;gap:8px" }, h("button", { class: "btn primary", onclick: rerun }, "Re-run with these inputs"), h("button", { class: "btn", onclick: reset }, "Reset")))),
  );

  // decision checks + inputs from the snapshot
  const recPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "IC screen checks"), h("span", { class: "faint" }, "KCI's criteria; not an investment decision")),
    h("div", { class: "pb flush" }, h("table", {}, h("tbody", {}, a.rec.checks.map((x) => h("tr", {}, h("td", {}, x.label), h("td", { class: x.ok ? "pos" : "neg" }, x.ok ? "pass" : "fail"), h("td", { class: "muted" }, x.detail)))))));
  const inPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Snapshot figures the engine read"), h("span", { class: "faint" }, "Click for provenance")),
    h("div", { class: "pb flush" }, h("table", {}, h("tbody", {}, v.inputs.map((f) => h("tr", { class: "clickable", onclick: () => openDrawer(f.title, f, "USD") },
      h("td", {}, f.title), h("td", { class: "n" }, fval(f.value, f.unit)), h("td", { class: "muted" }, unitText(f.unit, "USD")), h("td", {}, chips(f))))))));

  fill(body, kpis, status, h("div", { class: "grid cols-2" }, h("div", { class: "stack" }, dcfPanel, lboPanel), h("div", { class: "stack" }, cmp, recPanel, inPanel)));
}

// --- paper portfolio -----------------------------------------------------------------------------------------
// Display and data entry only. Sizing, fills, P&L and every risk check happen in Python (keers.portfolio,
// keers.risk); this code shows what the API returns and never decides whether a trade may go ahead.
const CHECK_TEXT = {
  "data.strategy": "Strategy tag", "data.trade_price": "Trade price fresh", "data.currency": "Currency",
  "data.book_prices": "Book prices fresh", "data.sector": "Sector known", "position.size": "Position size",
  "concentration.sector": "Sector concentration", "concentration.strategy": "Strategy concentration",
  "liquidity.trade": "Trade vs volume", "liquidity.position": "Position vs volume", "exposure.gross": "Gross exposure",
  "exposure.net": "Net exposure", "cash.no_borrowing": "No borrowing", "drawdown": "Drawdown",
};
const STATUS_LVL = { pass: "clear", warn: "watch", fail: "severe", unknown: "unknown" };
function checkBadge(st) { return h("span", { class: `lvl ${STATUS_LVL[st] || "unknown"}` }, st); }
function money(v, dp = 0) { return isNum(v) ? fnum(v, dp) : "—"; }
function pnlCell(v, dp = 0) { return h("td", { class: `n ${isNum(v) ? (v < 0 ? "neg" : v > 0 ? "pos" : "") : ""}` }, isNum(v) ? signed(fnum(v, dp), v) : "—"); }

function checksTable(checks) {
  return h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Check"), h("th", {}, "Result"), h("th", {}, "On"), h("th", {}, "Detail"))),
    h("tbody", {}, checks.map((c) => h("tr", {}, h("td", {}, CHECK_TEXT[c.name] || c.name), h("td", {}, checkBadge(c.status)),
      h("td", { class: "mono" }, c.subject || ""), h("td", { class: "muted" }, c.detail)))));
}

let PF = { id: null, data: null, list: null, prefill: null };

async function portfolioView(idPart, symPart) {
  document.title = "Paper portfolio · Keers & Co";
  fill(view, h("div", { class: "loading" }, "Loading the paper portfolio…"));
  let list;
  try { list = await api("/api/portfolios"); } catch (e) { fill(view, h("div", { class: "callout bad" }, e.message)); return; }
  if (idPart === "trade" && symPart) PF.prefill = { symbol: symPart.toUpperCase() };
  if (!list.portfolios.length) return createPortfolioView(list);
  const wanted = Number(idPart);
  const p = list.portfolios.find((x) => x.id === wanted) || list.portfolios[0];
  PF.id = p.id;
  return renderPortfolio(list, p.id);
}

function createPortfolioView(list) {
  const name = h("input", { value: "Paper", "aria-label": "Portfolio name", maxlength: 60 });
  const cash = h("input", { value: "1000000", inputmode: "decimal", "aria-label": "Starting cash" });
  const msg = h("p", { class: "err" });
  const go = async () => {
    try {
      const p = await api("/api/portfolios", { method: "POST", body: JSON.stringify({ name: name.value, startingCash: Number(cash.value) }) });
      location.hash = `#/portfolio/${p.id}`;
    } catch (e) { msg.textContent = e.message; }
  };
  fill(view, h("div", { class: "home-hero" }, h("h1", {}, "Paper portfolio"), h("p", {}, list.notice)),
    h("section", { class: "panel", style: "max-width:520px" },
      h("div", { class: "ph" }, h("h2", {}, "Start a paper portfolio")),
      h("div", { class: "pb form" },
        h("label", {}, "Name", name), h("label", {}, `Starting cash (${list.limits.base_currency})`, cash),
        h("button", { class: "btn primary", onclick: go }, "Create"), msg,
        h("p", { class: "faint" }, "Every trade is checked against the risk limits before it can fill. Orders are never deleted, so the record shows everything that was tried."))));
}

async function renderPortfolio(list, pid) {
  let d;
  try { d = await api(`/api/portfolios/${pid}`, { timeout: 90000 }); } catch (e) {
    fill(view, h("div", { class: "callout bad" }, h("strong", {}, "Could not load the portfolio. "), e.message,
      h("div", { style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: () => renderPortfolio(list, pid) }, "Retry"))));
    return;
  }
  PF.data = d; PF.list = list;
  const s = d.summary, r = d.risk, cur = d.portfolio.baseCurrency;
  const picker = list.portfolios.length > 1 ? h("select", { class: "sel", "aria-label": "Portfolio", onchange: (e) => { location.hash = `#/portfolio/${e.target.value}`; } },
    list.portfolios.map((p) => h("option", { value: p.id, selected: p.id === pid ? true : null }, p.name))) : null;

  const kpis = h("div", { class: "kpis" },
    kpi("Net asset value", money(s.nav), `${cur} · started with ${money(s.startingCash)}`),
    kpi("Total P&L", isNum(s.pnl) ? signed(fnum(s.pnl, 0), s.pnl) : "—", `${isNum(s.totalReturn) ? signed(fpct(s.totalReturn, 2), s.totalReturn) : "—"} · realised ${money(s.realised)} · unrealised ${money(s.unrealised)} · costs ${money(s.commissions)}`),
    kpi("Cash", money(s.cash), `${fpct(s.cashPct, 1)} of NAV`),
    kpi("Gross / net exposure", `${fpct(s.grossPct, 0)} / ${fpct(s.netPct, 0)}`, `long ${money(s.long)} · short ${money(s.short)}`),
    kpi("Drawdown", fpct(s.drawdown, 1), `worst ${fpct(s.maxDrawdown, 1)} · halt at ${fpct(d.limits.drawdown_halt, 0)}`),
  );

  const riskPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "Risk limits on the current book"), h("span", { class: "faint mono" }, r.limitsVersion)),
    h("div", { class: "pb" }, r.breaches.length
      ? h("div", { class: "callout bad" }, h("strong", {}, `${r.breaches.length} breach${r.breaches.length > 1 ? "es" : ""}. `), "Trades that add to a breach are blocked; trades that reduce it are allowed.",
        h("ul", {}, r.breaches.map((b) => h("li", {}, `${CHECK_TEXT[b.name] || b.name}${b.subject ? ` (${b.subject})` : ""}: ${b.detail}`))))
      : h("div", { class: "callout" }, h("strong", {}, "No breaches. "), "Every limit is within bounds and every price is fresh."),
      r.warnings.length ? h("div", { class: "callout warn", style: "margin-top:8px" }, h("ul", {}, r.warnings.map((w) => h("li", {}, `${CHECK_TEXT[w.name] || w.name}: ${w.detail}`)))) : null,
      h("details", { style: "margin-top:10px" }, h("summary", {}, `All checks (${r.checks.length})`), checksTable(r.checks)),
      h("details", { style: "margin-top:6px" }, h("summary", {}, "The limits"), limitsTable(d.limits, d.costs))));

  const posPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, `Positions (${d.positions.filter((x) => x.open).length} open)`), h("span", { class: "faint" }, "Marked at the latest Yahoo price")),
    h("div", { class: "pb flush scroll-x" }, d.positions.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Symbol", "Strategy", "Sector", "Shares", "Avg cost", "Price", "Market value", "Weight", "Unrealised", "Realised", "P&L", ""].map((t, i) => h("th", { class: i >= 3 && i <= 10 ? "n" : "" }, t)))),
      h("tbody", {}, d.positions.map((p) => h("tr", { class: p.open ? "" : "closed" },
        h("td", { class: "mono" }, h("a", { href: `#/c/${encodeURIComponent(p.symbol)}` }, p.symbol)),
        h("td", { title: d.strategies[p.strategy] }, p.strategy),
        h("td", { title: p.sectorBasis ? `sector from: ${p.sectorBasis}` : "" }, p.sector || "—"),
        h("td", { class: "n" }, fnum(p.quantity, 0)),
        h("td", { class: "n" }, isNum(p.avgCost) ? fnum(p.avgCost, 2) : "—"),
        h("td", { class: "n", title: [p.priceSource, p.priceAt, ...(p.priceWarnings || [])].filter(Boolean).join("\n") }, isNum(p.price) ? fnum(p.price, 2) : "—", " ",
          (p.priceWarnings || []).some((w) => /^stale|no price/i.test(w)) ? chip("stale") : null),
        h("td", { class: "n" }, money(p.marketValue)), h("td", { class: "n" }, fpct(p.weight, 1)),
        pnlCell(p.unrealised), pnlCell(p.realised), pnlCell(p.pnl),
        h("td", { class: "n" }, p.open ? [
          h("button", { class: "btn", onclick: () => ticketPrefill({ symbol: p.symbol, strategy: p.strategy, side: p.quantity > 0 ? "buy" : "sell" }) }, "Size"), " ",
          h("button", { class: "btn", onclick: () => ticketPrefill({ symbol: p.symbol, strategy: p.strategy, side: p.quantity > 0 ? "sell" : "buy", close: true }) }, "Close"),
        ] : h("span", { class: "faint" }, "closed")))))) : h("p", { class: "muted", style: "padding:12px" }, "No positions yet. Use the trade ticket.")));

  const expo = (title, obj, caps) => h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, title)),
    h("div", { class: "pb" }, Object.keys(obj).length ? Object.entries(obj).map(([k, v]) => {
      const cap = typeof caps === "number" ? caps : caps?.[k];
      const over = isNum(cap) && isNum(v.pct) && v.pct > cap;
      return h("div", { class: "bar-row" },
        h("span", { title: d.strategies[k] || "" }, k), h("span", { class: "bar" }, h("span", { class: over ? "fillx over" : "fillx", style: `width:${Math.min(100, Math.max(0, (v.pct || 0) * 100))}%` }),
          isNum(cap) ? h("span", { class: "cap", style: `left:${Math.min(100, cap * 100)}%`, title: `limit ${fpct(cap, 0)}` }) : null),
        h("span", { class: `mono ${over ? "neg" : ""}` }, fpct(v.pct, 1)));
    }) : h("p", { class: "muted" }, "Nothing held.")));

  const navPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, "NAV by day"), h("span", { class: "faint" }, "from stored closes")),
    h("div", { class: "pb" }, h("div", { class: "chart small", id: "nav-chart" })));

  const ordersPanel = h("section", { class: "panel" },
    h("div", { class: "ph" }, h("h2", {}, `Order record (${d.orders.length})`), h("span", { class: "faint" }, "Append-only; rejected attempts included")),
    h("div", { class: "pb flush scroll-x" }, d.orders.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["When", "Status", "Symbol", "Strat.", "Shares", "Price used", "Fill", "Costs", "Why / note"].map((t, i) => h("th", { class: i >= 4 && i <= 7 ? "n" : "" }, t)))),
      h("tbody", {}, d.orders.map((o) => h("tr", {},
        h("td", { class: "mono" }, fdatetime(o.created_at)), h("td", {}, checkBadge(o.status === "filled" ? "pass" : "fail"), " ", o.status),
        h("td", { class: "mono" }, o.symbol), h("td", {}, o.strategy), h("td", { class: "n" }, signed(fnum(o.quantity, 0), o.quantity)),
        h("td", { class: "n", title: `${o.price_source} ${o.price_at || ""}\nraw sha256 ${o.price_sha256 || "—"}` }, isNum(o.reference_price) ? fnum(o.reference_price, 2) : "—", h("div", { class: "faint" }, o.price_source)),
        h("td", { class: "n" }, isNum(o.fill_price) ? fnum(o.fill_price, 4) : "—"), h("td", { class: "n" }, isNum(o.commission) ? fnum(o.commission, 2) : "—"),
        h("td", { class: o.status === "rejected" ? "neg" : "muted" }, o.reason || o.note || "")))))
      : h("p", { class: "muted", style: "padding:12px" }, "No orders yet.")));

  const ticket = h("section", { class: "panel", id: "ticket" });
  fill(view,
    h("div", { class: "co-head" }, h("div", { class: "co-id" }, h("div", {}, h("span", { class: "ticker" }, d.portfolio.name), h("span", { class: "name" }, "Paper portfolio"), " ", picker),
      h("div", { class: "facts" }, h("span", {}, `${cur}`), h("span", {}, `opened ${fdate(d.portfolio.createdAt)}`), h("span", {}, `as of ${d.today}`))),
      h("div", {}, h("button", { class: "btn", onclick: () => renderPortfolio(list, pid) }, "Reload prices"))),
    h("div", { class: "callout", style: "margin-bottom:12px" }, d.notice),
    kpis,
    h("div", { class: "grid cols-2", style: "margin-top:16px" },
      h("div", { class: "stack" }, posPanel, navPanel, ordersPanel),
      h("div", { class: "stack" }, ticket, riskPanel, expo("Exposure by strategy (gross, % of NAV)", s.byStrategy, d.limits.max_strategy), expo("Exposure by sector (gross, % of NAV)", s.bySector, d.limits.max_sector))));
  renderTicket(ticket, d, list, pid);
  drawNav(d.history, s.nav, d.today);
}

function limitsTable(l, c) {
  const rows = [
    ["One position", fpct(l.max_position, 0) + " of NAV"], ["One sector", fpct(l.max_sector, 0) + " of NAV"],
    ["Strategy sleeves", Object.entries(l.max_strategy).map(([k, v]) => `${k} ${fpct(v, 0)}`).join(" · ")],
    ["One trade", `${fpct(l.max_trade_adv, 0)} of ${l.adv_days}-day average volume`], ["One position", `${fpct(l.max_position_adv, 0)} of average volume`],
    ["Gross exposure", `up to ${fpct(l.max_gross, 0)}`], ["Net exposure", `${fpct(l.min_net, 0)} to ${fpct(l.max_net, 0)}`],
    ["Drawdown", `warn at ${fpct(l.drawdown_warn, 0)}, halt new risk at ${fpct(l.drawdown_halt, 0)}`],
    ["Price age", `at most ${l.max_price_age_days} business days; a stale quote blocks`], ["Currency", `${l.base_currency} only (no FX yet)`],
    ["Fill costs", `${fnum(c.slippage_bps, 0)} bp slippage + ${fnum(c.commission_bps, 0)} bp commission (min ${fnum(c.min_commission, 2)}), ${c.version}`],
  ];
  return h("table", {}, h("tbody", {}, rows.map(([a, b]) => h("tr", {}, h("td", {}, a), h("td", { class: "muted" }, b)))));
}

function ticketPrefill(p) {
  PF.prefill = p;
  const box = $("#ticket");
  if (box && PF.data) renderTicket(box, PF.data, PF.list, PF.id);
  box?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderTicket(box, d, list, pid) {
  const pre = PF.prefill || {};
  PF.prefill = null;
  const sym = h("input", { value: pre.symbol || "", placeholder: "e.g. ADBE", "aria-label": "Symbol", autocomplete: "off", spellcheck: "false" });
  const strat = h("select", { "aria-label": "Strategy" }, Object.entries(d.strategies).map(([k, v]) => h("option", { value: k, selected: k === (pre.strategy || "B") ? true : null }, `${k}: ${v}`)));
  const side = h("select", { "aria-label": "Side" }, ["buy", "sell"].map((k) => h("option", { value: k, selected: k === (pre.side || "buy") ? true : null }, k === "buy" ? "Buy (or cover a short)" : "Sell (or short)")));
  const mode = h("select", { "aria-label": "Size by" }, [["weight", "% of NAV"], ["shares", "Shares"], ["amount", `Amount (${d.portfolio.baseCurrency})`]].map(([k, v]) => h("option", { value: k }, v)));
  const size = h("input", { inputmode: "decimal", placeholder: "e.g. 2.5", "aria-label": "Size" });
  const closeBox = h("input", { type: "checkbox", checked: pre.close ? true : null, "aria-label": "Close the whole position" });
  const sector = h("input", { placeholder: "from the listing or SIC code if blank", "aria-label": "Sector", maxlength: 60 });
  const note = h("input", { placeholder: "optional", maxlength: 200, "aria-label": "Note" });
  const out = h("div", { class: "scroll-x" });
  const place = h("button", { class: "btn primary", disabled: true }, "Place paper trade");
  let previewed = null;
  const body = () => {
    const b = { symbol: sym.value.trim().toUpperCase(), strategy: strat.value, side: side.value, sector: sector.value.trim() || null, note: note.value };
    if (closeBox.checked) b.close = true;
    else if (mode.value === "weight") b.weight = Number(size.value) / 100;
    else b[mode.value] = Number(size.value);
    return b;
  };
  const invalidate = () => { previewed = null; place.disabled = true; };
  for (const el of [sym, strat, side, mode, size, closeBox, sector]) el.addEventListener("input", invalidate);
  closeBox.addEventListener("change", () => { size.disabled = mode.disabled = closeBox.checked; });
  size.disabled = mode.disabled = closeBox.checked;

  const show = (r, done = false) => {
    const f = r.fill, m = r.mark;
    fill(out,
      done ? h("div", { class: r.filled ? "callout" : "callout bad", style: "margin-bottom:8px" }, h("strong", {}, r.filled ? `Filled on paper: ${signed(fnum(r.quantity, 0), r.quantity)} ${r.symbol}. ` : "Not filled. "),
        r.filled ? `Order ${r.order.id} recorded.` : "The risk checks blocked it, and the attempt is recorded.") : null,
      h("table", {}, h("tbody", {},
        h("tr", {}, h("td", {}, "Shares"), h("td", { class: "n" }, signed(fnum(r.quantity, 0), r.quantity)), h("td", { class: "muted" }, `position ${fnum(r.positionBefore, 0)} → ${fnum(r.positionAfter, 0)}`)),
        h("tr", {}, h("td", {}, "Price used"), h("td", { class: "n" }, isNum(m.price) ? fnum(m.price, 2) : "—"), h("td", { class: "muted" }, `${m.source}${m.at ? ` · ${m.at}` : ""}${m.currency ? ` · ${m.currency}` : ""}`)),
        (m.warnings || []).length ? h("tr", {}, h("td", {}, "Price notes"), h("td", { colspan: 2, class: "muted" }, m.warnings.join(" · "))) : null,
        h("tr", {}, h("td", {}, "Simulated fill"), h("td", { class: "n" }, f ? fnum(f.price, 4) : "—"), h("td", { class: "muted" }, f ? `${fnum(f.slippage_bps, 0)} bp slippage` : "no price to fill at")),
        h("tr", {}, h("td", {}, "Notional + commission"), h("td", { class: "n" }, f ? money(f.notional, 2) : "—"), h("td", { class: "muted" }, f ? `commission ${fnum(f.commission, 2)}` : "")),
        h("tr", {}, h("td", {}, "Average daily volume"), h("td", { class: "n" }, isNum(m.advShares) ? fnum(m.advShares, 0) : "unknown"), h("td", { class: "muted" }, `${m.advSessions} sessions`)),
        h("tr", {}, h("td", {}, "Sector"), h("td", { class: "n" }, r.sector || "unknown"), h("td", { class: "muted" }, r.sectorBasis ? `from: ${r.sectorBasis}` : "enter one above")),
      )),
      h("div", { style: "margin:10px 0 6px" }, r.risk.approved ? h("span", { class: "lvl clear" }, "risk checks pass") : h("span", { class: "lvl severe" }, "blocked by risk checks"),
        r.risk.missing.length ? h("span", { class: "err" }, ` missing checks: ${r.risk.missing.join(", ")}`) : null),
      r.risk.breaches.length ? h("div", { class: "callout bad" }, h("ul", {}, r.risk.breaches.map((b) => h("li", {}, `${CHECK_TEXT[b.name] || b.name}: ${b.detail}`)))) : null,
      r.risk.warnings.length ? h("div", { class: "callout warn", style: "margin-top:6px" }, h("ul", {}, r.risk.warnings.map((b) => h("li", {}, `${CHECK_TEXT[b.name] || b.name}: ${b.detail}`)))) : null,
      h("details", { style: "margin-top:8px" }, h("summary", {}, `All ${r.risk.checks.length} checks`), checksTable(r.risk.checks)));
  };
  const preview = async (btn) => {
    invalidate();
    btn.disabled = true; fill(out, h("div", { class: "loading" }, "Fetching the latest price and checking limits…"));
    try {
      const r = await api(`/api/portfolios/${pid}/preview`, { method: "POST", body: JSON.stringify(body()), timeout: 60000 });
      show(r);
      if (r.risk.approved && r.fill) { previewed = JSON.stringify(body()); place.disabled = false; }
    } catch (e) { fill(out, h("p", { class: "err" }, e.message)); } finally { btn.disabled = false; }
  };
  place.addEventListener("click", async () => {
    // The server runs every check again at the moment of the trade; this preview is only for the person.
    if (!previewed || previewed !== JSON.stringify(body())) { invalidate(); return; }
    place.disabled = true;
    try {
      const r = await api(`/api/portfolios/${pid}/trades`, { method: "POST", body: previewed, timeout: 60000 });
      show(r, true);
      setTimeout(() => renderPortfolio(list, pid), r.filled ? 900 : 2500);
    } catch (e) { fill(out, h("p", { class: "err" }, e.message)); }
  });
  const prevBtn = h("button", { class: "btn", onclick: (e) => preview(e.target) }, "Preview");
  fill(box, h("div", { class: "ph" }, h("h2", {}, "Trade ticket (paper)"), h("span", { class: "faint" }, "Preview first; the server checks again when you place it")),
    h("div", { class: "pb form" },
      h("label", {}, "Symbol", sym), h("label", {}, "Strategy", strat), h("label", {}, "Side", side),
      h("div", { class: "row2" }, h("label", {}, "Size by", mode), h("label", {}, "Size", size)),
      h("label", { class: "check" }, closeBox, " Close the whole position"),
      h("label", {}, "Sector", sector), h("label", {}, "Note", note),
      h("div", { style: "display:flex;gap:8px;margin-top:4px" }, prevBtn, place), out));
  if (pre.close) preview(prevBtn); // a close needs no size, so show its checks straight away
  else if (pre.symbol) size.focus();
}

function drawNav(history, nav, today) {
  const box = $("#nav-chart");
  if (chartHandle) { chartHandle.remove(); chartHandle = null; }
  if (!box) return;
  const pts = history.map((x) => ({ time: x.day, value: x.nav }));
  if (isNum(nav) && (!pts.length || pts[pts.length - 1].time < today)) pts.push({ time: today, value: nav });
  if (pts.length < 2 || !window.LightweightCharts) { fill(box, h("div", { class: "empty" }, "The chart starts once the book has two days of history.")); return; }
  const LW = window.LightweightCharts;
  const chart = LW.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: cssVar("--panel") }, textColor: cssVar("--muted"), fontFamily: cssVar("--mono"), fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: cssVar("--line") }, horzLines: { color: cssVar("--line") } },
    rightPriceScale: { borderColor: cssVar("--line-strong") }, timeScale: { borderColor: cssVar("--line-strong") },
  });
  chart.addSeries(LW.LineSeries, { color: cssVar("--accent"), lineWidth: 1.5, priceLineVisible: false }).setData(pts);
  chart.timeScale().fitContent();
  chartHandle = chart;
}

// --- start ---------------------------------------------------------------------------------------------------
loadStatus();
route();
