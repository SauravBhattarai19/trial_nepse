/* NEPSE Analyst: today's suggestions, screener, stock view, sector rotation, evidence.
 *
 * Reads data/screener.json and data/stocks/<SYM>.json, both written by
 * scripts/build_screener.py after every trading day. All scores, setups and
 * their track records are computed there; this file only draws them.
 *
 * Views live in the URL hash (#v=today|screener|stock|sectors|market|evidence,
 * q=<symbol> for the stock view). The "Market breadth" view is the original
 * chart in app.js, started lazily the first time it is opened; it keeps its own
 * hash keys (s, r, ma, ...) and leaves v / q alone.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LC = window.LightweightCharts;
  const VIEWS = ["today", "screener", "stock", "sectors", "market", "evidence"];
  const MARKET_KEYS = ["mf", "deb", "adl", "s", "adj", "r", "ma", "p", "sec", "sig", "per"];
  const STAGES = { 0: "–", 1: "1 · Basing", 2: "2 · Advancing", 3: "3 · Topping", 4: "4 · Declining" };
  const MAP_SCALE = { ch1: 3, ch5: 6, ch20: 12, ch60: 25 };

  let S = null;              // screener.json
  const bySym = {};
  const detailCache = new Map();
  const state = {
    view: "today", sym: null, mapWin: "ch1", sort: { k: "ai", d: -1 }, filters: new Set(), fq: "", fsec: "",
    lower: "vol", range: 250, over: { ma: true, st: true, bb: false, ev: true, sr: true },
  };

  // ---------------------------------------------------------------- storage
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const watch = new Set(store.get("nepse-watch", []));
  function toggleWatch(sym) {
    if (watch.has(sym)) watch.delete(sym); else watch.add(sym);
    store.set("nepse-watch", [...watch]);
    for (const b of document.querySelectorAll(`[data-star="${CSS.escape(sym)}"]`)) setStar(b, sym);
    renderWatch();
    if (state.view === "screener") renderScreener();
  }
  function setStar(b, sym) {
    const on = watch.has(sym);
    b.textContent = on ? "★" : "☆";
    b.setAttribute("aria-pressed", String(on));
    b.setAttribute("aria-label", (on ? "Remove " : "Add ") + sym + (on ? " from" : " to") + " watchlist");
  }
  const star = (sym) => `<button type="button" class="star" data-star="${esc(sym)}" aria-pressed="${watch.has(sym)}" aria-label="${watch.has(sym) ? "Remove from" : "Add to"} watchlist">${watch.has(sym) ? "★" : "☆"}</button>`;

  // ---------------------------------------------------------------- format
  const nf2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fin = (v) => v != null && isFinite(v);
  const fmt = (v) => (fin(v) ? nf2.format(v) : "–");
  const signed = (v, d = 1) => (fin(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) : "–");
  const pct = (v, d = 1) => (fin(v) ? signed(v, d) + "%" : "–");
  const arrow = (v) => (!fin(v) || v === 0 ? "" : v > 0 ? "▲ " : "▼ ");
  const cls = (v) => (!fin(v) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const lakh = (v) => (!fin(v) ? "–" : v >= 100 ? nf2.format(v / 100) + " Cr" : nf0.format(v) + " Lakh");
  const trendWord = (v) => (!fin(v) ? "–" : v >= 50 ? "Strong uptrend" : v >= 15 ? "Uptrend" : v > -15 ? "Sideways" : v > -50 ? "Downtrend" : "Strong downtrend");

  // ---------------------------------------------------------------- theme
  function theme() {
    const s = getComputedStyle(document.documentElement);
    const v = (n) => s.getPropertyValue(n).trim();
    return {
      surface: v("--surface"), grid: v("--grid"), border: v("--border"), muted: v("--muted"), ink: v("--ink"), ink2: v("--ink-2"),
      up: v("--up"), down: v("--down"), accent: v("--accent"), ma: [v("--ma1"), v("--ma2"), v("--ma3")], warn: v("--warn"),
      mid: v("--mid"), vol: v("--vol"),
    };
  }
  const hex = (h) => { h = h.replace("#", ""); if (h.length === 3) h = h.split("").map((c) => c + c).join(""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
  const toHex = (rgb) => "#" + rgb.map((x) => Math.round(Math.max(0, Math.min(255, x))).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => { const A = hex(a), B = hex(b); return toHex(A.map((x, i) => x + (B[i] - x) * t)); };
  function lum(h) { const c = hex(h).map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; }
  // diverging fill: down <- mid (grey) -> up, |v| / max saturates at 1
  function divColor(v, max, t) {
    if (!fin(v)) return t.mid;
    const k = Math.min(Math.abs(v) / max, 1);
    return mix(t.mid, v >= 0 ? t.up : t.down, 0.15 + 0.85 * k);
  }
  const inkOn = (bg) => (lum(bg) > 0.36 ? "#0b0b0b" : "#ffffff");

  // ---------------------------------------------------------------- tooltip
  const tip = $("tip");
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  const hideTip = () => { tip.hidden = true; };
  document.addEventListener("pointerover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) showTip(el.dataset.tip, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", (e) => {
    if (tip.hidden) return;
    const el = e.target.closest("[data-tip]");
    if (el) showTip(el.dataset.tip, e.clientX, e.clientY); else hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest("[data-tip]");
    if (!el) return;
    const r = el.getBoundingClientRect();
    showTip(el.dataset.tip, r.left, r.bottom);
  });
  document.addEventListener("focusout", hideTip);
  window.addEventListener("scroll", hideTip, { passive: true });

  // ---------------------------------------------------------------- small marks
  function ring(v, size = 44) {
    if (!fin(v)) return `<span class="ring-na" style="width:${size}px;height:${size}px" data-tip="No AI score: thinly traded or listed recently">–</span>`;
    const r = size / 2 - 4, c = 2 * Math.PI * r;
    return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="AI score ${v} of 99" data-tip="AI score <b>${v}</b>/99 · higher = the model expects it to beat the average stock over the next 20 sessions">` +
      `<circle class="trk" cx="${size / 2}" cy="${size / 2}" r="${r}"/>` +
      `<circle class="val" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${(c * (1 - v / 100)).toFixed(1)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>` +
      `<text x="50%" y="50%" dy=".35em" text-anchor="middle" style="font-size:${size > 50 ? 18 : size > 36 ? 14 : 11}px">${v}</text></svg>`;
  }
  function trendBar(v) {
    if (!fin(v)) return `<span class="tbar"></span>`;
    const w = Math.abs(v) / 2;
    return `<span class="tbar" data-tip="${trendWord(v)} · trend rating ${signed(v, 0)} (−100 to +100, votes of 7 moving averages and 5 oscillators)">` +
      `<span class="tb-fill ${v >= 0 ? "up" : "dn"}" style="width:${w}%;${v >= 0 ? "left:50%" : "right:50%"}"></span><span class="tb-zero"></span></span>`;
  }
  function spark(arr, w = 120, h = 34) {
    const v = (arr || []).filter(fin);
    if (v.length < 2) return `<svg class="spark" width="${w}" height="${h}"></svg>`;
    let lo = Math.min(...v), hi = Math.max(...v);
    if (hi === lo) { hi += 1; lo -= 1; }
    const pts = v.map((y, i) => `${((i / (v.length - 1)) * (w - 6) + 1).toFixed(1)},${(h - 3 - ((y - lo) / (hi - lo)) * (h - 6)).toFixed(1)}`);
    const last = pts[pts.length - 1].split(",");
    const dir = v[v.length - 1] >= v[0] ? "up" : "dn";
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts.join(" ")}"/><circle class="end ${dir}" cx="${last[0]}" cy="${last[1]}" r="3"/></svg>`;
  }
  const VERDICT = {
    up: ["▲", "Beat the market after"],
    down: ["▼", "Lagged the market after"],
    none: ["●", "No consistent edge"],
    thin: ["●", "Too few cases to judge"],
  };
  function verdictLine(e) {
    const h = S.model.halves;
    const part = (k, lab) => (e[k].ex == null ? "" : `${lab}: <b>${pct(e[k].ex, 2)}</b> vs the average stock over 20 sessions, beat it ${e[k].hit}% of the time (n=${nf0.format(e[k].n)})`);
    return `${VERDICT[e.verdict][1]} on NEPSE.<br>${part("h2", h.h2)}<br>${part("h1", h.h1)}`;
  }
  function evChip(code, ago) {
    const e = S.events[code];
    if (!e) return "";
    const surprise = (e.textbook > 0 && e.verdict === "down") || (e.textbook < 0 && e.verdict === "up");
    return `<span class="evc v-${e.verdict}" data-tip="<b>${esc(e.label)}</b>: ${esc(e.desc)}.<br>Textbook: ${e.textbook > 0 ? "bullish" : "bearish"}.${surprise ? " <b>NEPSE did the opposite.</b>" : ""}<br>${verdictLine(e)}">` +
      `<span class="g">${VERDICT[e.verdict][0]}</span>${esc(e.label)}${ago ? ` <small>${ago}d</small>` : ""}</span>`;
  }

  // ---------------------------------------------------------------- router
  function readRoute() {
    const p = new URLSearchParams(location.hash.slice(1));
    let v = p.get("v");
    if (!VIEWS.includes(v)) v = MARKET_KEYS.some((k) => p.has(k)) ? "market" : "today";   // old breadth links
    return { v, q: p.get("q") ? p.get("q").toUpperCase() : null };
  }
  function go(view, sym) {
    const p = new URLSearchParams(location.hash.slice(1));
    p.set("v", view);
    if (sym) p.set("q", sym);
    else if (view !== "stock") p.delete("q");
    const h = "#" + p.toString().replace(/%2C/g, ",").replace(/%3A/g, ":").replace(/%7C/g, "|");
    if (h === location.hash) route(); else location.hash = h;
  }
  function route() {
    const { v, q } = readRoute();
    const changed = v !== state.view;
    state.view = v;
    for (const name of VIEWS) $("v-" + name).hidden = name !== v;
    for (const a of document.querySelectorAll(".tabs a")) a.setAttribute("aria-current", a.dataset.view === v ? "page" : "false");
    if (v === "market") window.NepseMarket && window.NepseMarket.start();
    if (v === "today" && S) { renderMap(); renderWatch(); }
    if (v === "screener") renderScreener();
    if (v === "sectors") renderSectors();
    if (v === "evidence") renderEvidence();
    if (v === "stock") openStock(q || state.sym || (S.picks[0] && S.picks[0].s));
    if (changed) window.scrollTo(0, 0);
    hideTip();
  }

  // ---------------------------------------------------------------- header
  function renderFresh() {
    const npt = (ms) => new Date(ms + (5 * 60 + 45) * 60000).toISOString();
    const today = npt(Date.now()).slice(0, 10);
    const days = Math.round((Date.parse(today) - Date.parse(S.last_date)) / 86400000);
    const wd = new Date(S.last_date + "T00:00:00Z").toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
    const g = npt(Date.parse(S.generated_at));
    const el = $("fresh");
    el.className = "fresh" + (days > 4 ? " stale" : "");
    el.textContent = `Data through ${wd} ${S.last_date} · analysed ${g.slice(0, 10) === today ? "today" : g.slice(0, 10)} ${g.slice(11, 16)} NPT` +
      (days > 4 ? ` · no new trading day for ${days} days` : "");
  }

  // ---------------------------------------------------------------- today
  function renderRegime() {
    const r = S.regime, sc = r.score;
    const word = sc == null ? "–" : sc >= 4 ? "Bullish breadth" : sc <= 1 ? "Bearish breadth" : "Mixed breadth";
    const tot = (r.adv || 0) + (r.dec || 0);
    const lead = S.sectors.filter((x) => x.q === "Leading").map((x) => x.name);
    const imp = S.sectors.filter((x) => x.q === "Improving" && x.q_prev === "Lagging").map((x) => x.name);
    $("regime").innerHTML =
      `<div class="rg"><span class="k">NEPSE · ${esc(r.nepse_date)}</span><span class="v">${fmt(r.nepse)}</span>` +
      `<span class="d ${cls(r.nepse_chg)}">${arrow(r.nepse_chg)}${pct(r.nepse_chg, 2)} today · ${pct(r.nepse_5d, 1)} 1W</span></div>` +
      `<div class="rg"><span class="k">Breadth score</span><span class="v">${sc == null ? "–" : sc}<small>/5</small></span>` +
      `<span class="meter5" aria-hidden="true">${[0, 1, 2, 3, 4].map((k) => `<i class="${k < sc ? "on" : ""}"></i>`).join("")}</span><span class="d">${word}</span></div>` +
      `<div class="rg"><span class="k">Shares up / down</span><span class="v">${r.adv ?? "–"} <small>▲</small> ${r.dec ?? "–"} <small>▼</small></span>` +
      `<span class="split2" data-tip="${r.adv} shares rose, ${r.dec} fell today (mutual funds and debentures not counted)"><i style="width:${tot ? (r.adv / tot) * 100 : 50}%"></i></span></div>` +
      `<div class="rg"><span class="k">Money flow, 10-day</span><span class="v">${r.flow == null ? "–" : Math.round(r.flow) + "%"}</span>` +
      `<span class="d">${r.flow >= 50 ? "more turnover in rising stocks" : "more turnover in falling stocks"}</span></div>` +
      `<div class="rg"><span class="k">Sector rotation</span><span class="v sm">${lead.length ? esc(lead.join(", ")) : "No sector leading"}</span>` +
      `<span class="d">${imp.length ? "turning up: " + esc(imp.join(", ")) : "leading the average stock"}</span></div>`;
  }

  let newPicks = new Set();
  function computeSeen() {
    const seen = store.get("nepse-seen", null);
    const picks = S.picks.map((x) => x.s);
    let prev = null;
    if (seen && seen.date < S.last_date) prev = { date: seen.date, picks: seen.picks || [] };
    else if (seen && seen.date === S.last_date) prev = seen.prev || null;
    newPicks = prev ? new Set(picks.filter((s) => !prev.picks.includes(s))) : new Set();
    store.set("nepse-seen", { date: S.last_date, picks, prev });
    return { first: !seen, prev };
  }
  function watchAlerts(st) {
    const out = [];
    for (const [code, ago] of st.ev) if (ago <= 1) out.push(evChip(code, ago));
    if (fin(st.ai) && st.ai <= 15) out.push(`<span class="evc v-down" data-tip="The AI rates it among the weakest 15% today">▼ AI score ${st.ai}</span>`);
    if (fin(st.ai) && st.ai >= 90) out.push(`<span class="evc v-up" data-tip="The AI rates it among the strongest 10% today">▲ AI score ${st.ai}</span>`);
    return out;
  }
  function renderBanner(seen) {
    const b = $("banner");
    if (store.get("nepse-banner", "") === S.last_date) { b.hidden = true; return; }
    const nSetups = S.setups.reduce((x, s) => x + s.syms.length, 0);
    const alerts = [...watch].filter((s) => bySym[s] && watchAlerts(bySym[s]).length);
    let html;
    if (seen.first) {
      html = `<b>New here?</b> After every trading day the AI ranks every liquid NEPSE stock, scans ${Object.keys(S.events).length} textbook setups and checks how each one has actually done on NEPSE. ` +
        `Start with the cards below, tap <b>☆</b> on stocks you hold to get alerts here, or see <a href="#v=evidence">how accurate it has been</a>.`;
    } else {
      const parts = [];
      if (seen.prev && newPicks.size) parts.push(`<b>${newPicks.size}</b> new in the AI top-rated list`);
      parts.push(`<b>${nSetups}</b> setups fired`);
      if (alerts.length) parts.push(`<b>${alerts.length}</b> watchlist alert${alerts.length > 1 ? "s" : ""}: ${alerts.map((s) => `<a href="#v=stock&q=${encodeURIComponent(s)}">${esc(s)}</a>`).join(", ")}`);
      html = `<b>${seen.prev ? `Since your last visit (${esc(seen.prev.date)})` : "Today"}:</b> ${parts.join(" · ")}.`;
    }
    b.innerHTML = `<span class="bi" aria-hidden="true">✦</span><p>${html}</p><button type="button" class="x" aria-label="Dismiss">×</button>`;
    b.hidden = false;
    b.querySelector(".x").onclick = () => { store.set("nepse-banner", S.last_date); b.hidden = true; };
  }

  function renderWatch() {
    const list = [...watch].filter((s) => bySym[s]);
    $("watch-block").hidden = !list.length;
    if (!list.length) return;
    $("watch").innerHTML = list.map((s) => {
      const st = bySym[s], al = watchAlerts(st);
      return `<div class="wrow" data-sym="${esc(s)}" tabindex="0" role="link" aria-label="Open ${esc(s)}">${star(s)}<b>${esc(s)}</b>` +
        `<span class="px">${fmt(st.c)} <span class="${cls(st.ch1)}">${arrow(st.ch1)}${pct(st.ch1, 2)}</span></span>${ring(st.ai, 30)}` +
        `<span class="tw">${trendWord(st.tr)}</span><span class="al">${al.length ? al.join("") : `<span class="quiet">no new signals</span>`}</span></div>`;
    }).join("");
  }

  function why(list) {
    return (list || []).map(([w, t]) => `<li>${esc(w)}${t ? ` <small>${esc(t)}</small>` : ""}</li>`).join("");
  }
  function evList(st, max = 3) {
    return st.ev.slice(0, max).map(([c, a]) => evChip(c, a)).join("");
  }
  function card(x) {
    const st = bySym[x.s];
    if (!st) return "";
    return `<div class="scard" data-sym="${esc(x.s)}" tabindex="0" role="link" aria-label="Open ${esc(x.s)}">` +
      `<div class="sc-top"><div class="nm"><b>${esc(x.s)}</b>${newPicks.has(x.s) ? `<span class="new">new</span>` : ""}${star(x.s)}<small>${esc(st.sec)}</small></div>${ring(st.ai, 46)}</div>` +
      `<div class="sc-px"><b>${fmt(st.c)}</b> <span class="${cls(st.ch1)}">${arrow(st.ch1)}${pct(st.ch1, 2)}</span><span class="m">1M ${pct(st.ch20)}</span></div>` +
      spark(st.sp, 220, 40) +
      `<div class="sc-meta">${trendBar(st.tr)}<span>${trendWord(st.tr)}</span><span data-tip="IBD-style relative strength rating, 1–99">RS ${st.rs ?? "–"}</span><span data-tip="Minervini trend-template rules met">${st.tt ?? "–"}/8</span></div>` +
      `<ul class="why">${why(x.why)}</ul><div class="sc-ev">${evList(st)}</div></div>`;
  }
  function row(x, kind) {
    const st = bySym[x.s];
    if (!st) return "";
    let tag = "";
    if (kind === "leader" && fin(st.ai)) tag = st.ai >= 60 ? `<span class="agree yes" data-tip="The AI also rates it well">✓ AI agrees</span>` : st.ai <= 40 ? `<span class="agree no" data-tip="Strong chart, but the AI expects it to lag: see the reasons">⚠ AI disagrees</span>` : `<span class="agree mid">AI neutral</span>`;
    return `<div class="srow" data-sym="${esc(x.s)}" tabindex="0" role="link" aria-label="Open ${esc(x.s)}">` +
      `<div class="nm">${star(x.s)}<b>${esc(x.s)}</b><small>${esc(st.sec)}</small></div>` +
      `<div class="sp">${spark(st.sp, 96, 30)}</div>` +
      `<div class="px"><b>${fmt(st.c)}</b><span class="${cls(st.ch20)}">1M ${pct(st.ch20)}</span></div>` +
      `<div class="tr">${trendBar(st.tr)}<small>${trendWord(st.tr)}</small></div>` +
      `<div class="ai">${ring(st.ai, 36)}</div>` +
      `<div class="wy">${tag}<ul class="why">${why((x.why || []).slice(0, 3))}</ul>${evList(st, 3)}</div></div>`;
  }
  function renderToday() {
    renderRegime();
    $("picks-sub").textContent = `liquid stocks (median turnover ≥ ${S.pick_liq_min} lakh/day), at most ${2} per sector`;
    $("picks").innerHTML = S.picks.map(card).join("");
    $("leaders").innerHTML = S.leaders.map((x) => row(x, "leader")).join("");
    $("avoid").innerHTML = S.avoid.map((x) => row(x, "avoid")).join("");
    renderSetups();
    renderMap();
    renderWatch();
  }
  function renderSetups() {
    if (!S.setups.length) { $("setups").innerHTML = `<p class="quiet">No setups fired on the latest trading day.</p>`; return; }
    const h = S.model.halves;
    $("setups").innerHTML = S.setups.map(({ code, syms }) => {
      const e = S.events[code];
      const surprise = (e.textbook > 0 && e.verdict === "down") || (e.textbook < 0 && e.verdict === "up");
      return `<div class="setup v-${e.verdict}">` +
        `<div class="su-h"><span class="vb v-${e.verdict}">${VERDICT[e.verdict][0]}</span><div><b data-tip="${esc(e.desc)}">${esc(e.label)}</b>` +
        `<small>${VERDICT[e.verdict][1]}${surprise ? " (textbook says the opposite)" : ""} · ${h.h2}: ${pct(e.h2.ex, 2)} vs avg, n=${nf0.format(e.h2.n)}</small></div></div>` +
        `<div class="su-syms">${syms.map((s) => { const st = bySym[s]; return `<button type="button" class="sym" data-sym="${esc(s)}" data-tip="${esc(s)} · ${esc(st.sec)} · AI ${st.ai ?? "–"} · ${trendWord(st.tr)}">${esc(s)}${fin(st.ai) ? `<small>${st.ai}</small>` : ""}</button>`; }).join("")}</div></div>`;
    }).join("");
  }

  // squarified treemap (Bruls, Huizing & van Wijk)
  function squarify(items, x, y, w, h) {
    const out = [];
    const total = items.reduce((s, it) => s + it.v, 0);
    if (!total || w <= 0 || h <= 0) return out;
    const scale = (w * h) / total;
    const rest = items.map((it) => ({ it, a: it.v * scale }));
    let row = [];
    const worst = (r, len) => {
      const s = r.reduce((a, b) => a + b.a, 0), mx = Math.max(...r.map((q) => q.a)), mn = Math.min(...r.map((q) => q.a));
      return Math.max((len * len * mx) / (s * s), (s * s) / (len * len * mn));
    };
    const layRow = (r) => {
      const s = r.reduce((a, b) => a + b.a, 0);
      if (w >= h) {
        const cw = s / h; let cy = y;
        for (const q of r) { const ch = q.a / cw; out.push({ it: q.it, x, y: cy, w: cw, h: ch }); cy += ch; }
        x += cw; w -= cw;
      } else {
        const ch = s / w; let cx = x;
        for (const q of r) { const cw = q.a / ch; out.push({ it: q.it, x: cx, y, w: cw, h: ch }); cx += cw; }
        y += ch; h -= ch;
      }
    };
    while (rest.length) {
      const len = Math.min(w, h), next = rest[0];
      if (!row.length || worst(row.concat(next), len) <= worst(row, len)) { row.push(next); rest.shift(); }
      else { layRow(row); row = []; }
    }
    if (row.length) layRow(row);
    return out;
  }
  function renderMap() {
    const el = $("map"), t = theme(), key = state.mapWin, max = MAP_SCALE[key];
    for (const b of document.querySelectorAll("#map-win button")) b.setAttribute("aria-pressed", String(b.dataset.w === key));
    const W = el.clientWidth || 900, H = W < 640 ? 460 : 540;
    el.style.height = H + "px";
    const live = S.stocks.filter((s) => s.d === S.last_date && fin(s.liq) && s.liq > 0);
    const secs = {};
    for (const s of live) (secs[s.sec] = secs[s.sec] || []).push({ v: Math.sqrt(s.liq), s });
    const groups = Object.entries(secs).map(([name, items]) => ({ v: items.reduce((a, b) => a + b.v, 0), name, items: items.sort((a, b) => b.v - a.v) }))
      .sort((a, b) => b.v - a.v);
    let html = "";
    for (const g of squarify(groups, 0, 0, W, H)) {
      const head = g.h > 46 && g.w > 60 ? 17 : 0;
      html += `<div class="tm-sec" style="left:${g.x}px;top:${g.y}px;width:${g.w}px;height:${g.h}px">${head ? `<span class="tm-sn">${esc(g.it.name)}</span>` : ""}</div>`;
      for (const c of squarify(g.it.items, g.x + 1, g.y + head + 1, g.w - 2, g.h - head - 2)) {
        const s = c.it.s, v = s[key], bg = divColor(v, max, t), fg = inkOn(bg);
        const lab = c.w > 34 && c.h > 15, val = c.w > 40 && c.h > 30;
        html += `<div class="tm" data-sym="${esc(s.s)}" style="left:${c.x}px;top:${c.y}px;width:${Math.max(0, c.w - 2)}px;height:${Math.max(0, c.h - 2)}px;background:${bg};color:${fg}" ` +
          `data-tip="<b>${esc(s.s)}</b> · ${esc(s.sec)}<br>${fmt(s.c)} · ${{ ch1: "1D", ch5: "1W", ch20: "1M", ch60: "3M" }[key]} <b>${pct(v, key === "ch1" ? 2 : 1)}</b><br>Turnover ${lakh(s.liq)}/day · AI ${s.ai ?? "–"}">` +
          (lab ? `<span>${esc(s.s)}</span>` : "") + (val ? `<small>${pct(v, key === "ch1" ? 2 : 1)}</small>` : "") + `</div>`;
      }
    }
    el.innerHTML = html;
    const stops = [-1, -0.5, 0, 0.5, 1].map((k) => divColor(k * max, max, t));
    $("map-scale").innerHTML = `<span>${pct(-max, 0)}</span><i style="background:linear-gradient(90deg,${stops.join(",")})"></i><span>${pct(max, 0)}</span>`;
  }

  // ---------------------------------------------------------------- screener
  const COLS = [
    ["s", "Symbol"], ["c", "Price"], ["ch1", "1D"], ["ch5", "1W"], ["ch20", "1M"], ["ch60", "3M"], ["ch250", "1Y"],
    ["ai", "AI"], ["tr", "Trend"], ["rs", "RS"], ["tt", "Template"], ["stg", "Stage"], ["rsi", "RSI"], ["rv", "Rel vol"], ["ev", "Setups (5d)"], ["sp", "90 days"],
  ];
  const COL_TIP = {
    ai: "AI score 1–99: expected 20-session return vs the average stock, ranked", tr: "Trend rating −100..100 (moving-average + oscillator votes)",
    rs: "IBD-style relative strength 1–99", tt: "Minervini trend-template rules met, of 8", stg: "Weinstein stage on the 30-week average",
    rsi: "RSI 14", rv: "Today's turnover vs its 20-day average", ev: "Setups that fired in the last 5 sessions",
  };
  function passes(st) {
    const f = state.filters;
    if (state.fq && !st.s.includes(state.fq)) return false;
    if (state.fsec && st.sec !== state.fsec) return false;
    if (f.has("ai70") && !(st.ai >= 70)) return false;
    if (f.has("up") && !(st.tr >= 15)) return false;
    if (f.has("tt") && st.tt !== 8) return false;
    if (f.has("s2") && st.stg !== 2) return false;
    if (f.has("setup") && !st.ev.length) return false;
    if (f.has("liq") && !(st.liq >= S.pick_liq_min)) return false;
    if (f.has("watch") && !watch.has(st.s)) return false;
    return true;
  }
  function renderScreener() {
    const { k, d } = state.sort;
    const val = (st) => (k === "ev" ? st.ev.length : k === "s" ? st.s : st[k]);
    const rows = S.stocks.filter(passes).sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null || (typeof x === "number" && !isFinite(x))) return 1;
      if (y == null || (typeof y === "number" && !isFinite(y))) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * d;
    });
    $("f-count").textContent = `${rows.length} of ${S.stocks.length} stocks`;
    for (const b of document.querySelectorAll("#f-tags .tog")) b.setAttribute("aria-pressed", String(state.filters.has(b.dataset.f)));
    const t = theme();
    const heat = (v, m) => (fin(v) ? ` style="background:${divColor(v, m, t)}22"` : "");
    const head = `<thead><tr><th class="st"></th>` + COLS.map(([key, lab]) =>
      key === "sp" ? `<th>${lab}</th>` :
        `<th${COL_TIP[key] ? ` data-tip="${COL_TIP[key]}"` : ""}><button type="button" class="sort${k === key ? " on" : ""}" data-sort="${key}">${lab}${k === key ? (d < 0 ? " ↓" : " ↑") : ""}</button></th>`).join("") + `</tr></thead>`;
    const body = rows.map((st) => `<tr data-sym="${esc(st.s)}" tabindex="0">` +
      `<td class="st">${star(st.s)}</td>` +
      `<td class="sy"><b>${esc(st.s)}</b><small>${esc(st.sec)}${st.d !== S.last_date ? " · last traded " + esc(st.d) : ""}</small></td>` +
      `<td>${fmt(st.c)}</td>` +
      ["ch1", "ch5", "ch20", "ch60", "ch250"].map((c, i) => `<td class="${cls(st[c])}"${heat(st[c], [3, 6, 12, 25, 50][i])}>${pct(st[c], c === "ch1" ? 2 : 1)}</td>`).join("") +
      `<td>${fin(st.ai) ? `<span class="aic"><span class="aibar"><i style="width:${st.ai}%"></i></span><b>${st.ai}</b></span>` : `<span class="quiet" data-tip="Thinly traded or too new to score">–</span>`}</td>` +
      `<td class="trc">${trendBar(st.tr)}</td>` +
      `<td>${st.rs ?? "–"}</td><td>${st.tt == null ? "–" : st.tt + "/8"}</td><td data-tip="${STAGES[st.stg]}">${st.stg || "–"}</td>` +
      `<td>${st.rsi ?? "–"}</td><td>${fin(st.rv) ? st.rv.toFixed(1) + "×" : "–"}</td>` +
      `<td class="evs">${st.ev.map(([c, a]) => { const e = S.events[c]; return `<span class="evi v-${e.verdict}" data-tip="<b>${esc(e.label)}</b> ${a ? a + " sessions ago" : "today"}<br>${verdictLine(e)}">${VERDICT[e.verdict][0]}</span>`; }).join("")}</td>` +
      `<td class="spc">${spark(st.sp, 84, 24)}</td></tr>`).join("");
    $("scr").innerHTML = head + `<tbody>${body}</tbody>`;
  }

  // ---------------------------------------------------------------- stock view
  let sc = null;   // stock charts
  async function loadDetail(sym) {
    if (detailCache.has(sym)) return detailCache.get(sym);
    const res = await fetch("data/stocks/" + encodeURIComponent(sym) + ".json");
    if (!res.ok) throw new Error("no data for " + sym);
    const d = await res.json();
    detailCache.set(sym, d);
    return d;
  }
  function chartOpts(t, showTime, fmtPrices = true) {
    return {
      layout: { background: { type: "solid", color: t.surface }, textColor: t.muted, fontSize: 11, attributionLogo: showTime,
        fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif' },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.border, minimumWidth: 70, scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderColor: t.border, visible: showTime, rightOffset: 3 },
      crosshair: { mode: LC.CrosshairMode.Normal, vertLine: { color: t.muted, labelBackgroundColor: t.ink }, horzLine: { color: t.muted, labelBackgroundColor: t.ink } },
      handleScroll: { vertTouchDrag: false },
      localization: fmtPrices ? { priceFormatter: (p) => nf2.format(p) } : {},
    };
  }
  function makeStockCharts() {
    const t = theme();
    const mk = (id, time) => { const el = $(id); const c = LC.createChart(el, chartOpts(t, time, !time)); c.resize(el.clientWidth, el.clientHeight); return c; };
    const pf = (precision) => ({ priceFormat: { type: "price", precision, minMove: Math.pow(10, -precision) } });
    const c1 = mk("st-c1", false), c2 = mk("st-c2", true);
    const line = (c, color, w = 1.5, style = LC.LineStyle.Solid, extra = {}) => c.addLineSeries(Object.assign({ color, lineWidth: w, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, extra));
    const s = {
      bbu: line(c1, t.muted, 1, LC.LineStyle.Dotted), bbl: line(c1, t.muted, 1, LC.LineStyle.Dotted),
      candles: c1.addCandlestickSeries({ upColor: "rgba(0,0,0,0)", borderUpColor: t.up, wickUpColor: t.up, downColor: t.down, borderDownColor: t.down, wickDownColor: t.down, priceLineVisible: true, priceLineColor: t.muted }),
      m20: line(c1, t.ma[0]), m50: line(c1, t.ma[1]), m200: line(c1, t.ma[2], 1.5, LC.LineStyle.Dashed),
      stu: line(c1, t.up, 2), std: line(c1, t.down, 2),
      vol: c2.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "volume" } }),
      rsi: line(c2, t.accent, 2, LC.LineStyle.Solid, Object.assign({ lastValueVisible: true, crosshairMarkerVisible: true }, pf(0))),
      mh: c2.addHistogramSeries(Object.assign({ priceLineVisible: false, lastValueVisible: false }, pf(2))),
      macd: line(c2, t.accent, 1.5, LC.LineStyle.Solid, pf(2)), sig: line(c2, t.ma[1], 1.5, LC.LineStyle.Solid, pf(2)),
      ai: line(c2, t.accent, 2, LC.LineStyle.Solid, Object.assign({ lastValueVisible: true, crosshairMarkerVisible: true }, pf(0))),
      rel: line(c2, t.ink, 2, LC.LineStyle.Solid, Object.assign({ lastValueVisible: true, crosshairMarkerVisible: true }, pf(1))),
    };
    const pl = (sr, price, title) => sr.createPriceLine({ price, color: t.muted, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: false, title });
    pl(s.rsi, 70, "70"); pl(s.rsi, 30, "30"); pl(s.ai, 50, "50"); pl(s.rel, 100, "");
    let sync = false;
    for (const [a, b] of [[c1, c2], [c2, c1]]) {
      a.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (sync || !r) return; sync = true; b.timeScale().setVisibleLogicalRange(r); sync = false; });
    }
    const ro = new ResizeObserver(() => {
      if (!$("st-c1").clientWidth) return;   // view hidden
      const r = c1.timeScale().getVisibleLogicalRange();
      for (const [id, c] of [["st-c1", c1], ["st-c2", c2]]) { const el = $(id); c.resize(el.clientWidth, el.clientHeight); }
      if (r) c1.timeScale().setVisibleLogicalRange(r);
    });
    ro.observe($("v-stock"));
    sc = { c1, c2, s, srLines: [], doc: null, ro };
    const onMove = (src) => (p) => {
      const i = p && p.logical != null && p.time !== undefined ? Math.round(p.logical) : null;
      stockLegend(i);
      if (sync) return;
      sync = true;
      const other = src === c1 ? c2 : c1;
      if (i == null || !sc.doc || i < 0 || i >= sc.doc.d.length) other.clearCrosshairPosition();
      else if (src === c1) { const [sr, v] = lowerAnchor(i); if (v == null) other.clearCrosshairPosition(); else other.setCrosshairPosition(v, sc.doc.d[i], sr); }
      else other.setCrosshairPosition(sc.doc.c[i], sc.doc.d[i], s.candles);
      sync = false;
    };
    c1.subscribeCrosshairMove(onMove(c1));
    c2.subscribeCrosshairMove(onMove(c2));
  }
  function lowerAnchor(i) {
    const d = sc.doc, s = sc.s;
    return { vol: [s.vol, d.v[i]], rsi: [s.rsi, d.rsi[i]], macd: [s.macd, d.macd[i]], ai: [s.ai, d.ai[i]], rel: [s.rel, d.rel[i]] }[state.lower];
  }
  const ld = (d, arr) => d.d.map((time, i) => (fin(arr[i]) ? { time, value: arr[i] } : { time }));
  function setStockData(d) {
    const t = theme(), s = sc.s;
    s.candles.setData(d.d.map((time, i) => ({ time, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] })));
    s.m20.setData(ld(d, d.m20)); s.m50.setData(ld(d, d.m50)); s.m200.setData(ld(d, d.m200));
    s.stu.setData(ld(d, d.st.map((v, i) => (d.sd[i] === 1 ? v : null))));
    s.std.setData(ld(d, d.st.map((v, i) => (d.sd[i] === -1 ? v : null))));
    s.bbu.setData(ld(d, d.bu)); s.bbl.setData(ld(d, d.bl));
    s.vol.setData(d.d.map((time, i) => ({ time, value: d.v[i] || 0, color: (i && d.c[i] < d.c[i - 1] ? t.down : t.up) + "88" })));
    s.rsi.setData(ld(d, d.rsi));
    s.macd.setData(ld(d, d.macd)); s.sig.setData(ld(d, d.sig));
    s.mh.setData(d.d.map((time, i) => (fin(d.macd[i]) && fin(d.sig[i]) ? { time, value: d.macd[i] - d.sig[i], color: (d.macd[i] >= d.sig[i] ? t.up : t.down) + "99" } : { time })));
    s.ai.setData(ld(d, d.ai));
    s.rel.setData(ld(d, d.rel));
    applyOverlays();
    setLower(state.lower);
  }
  function applyOverlays() {
    if (!sc || !sc.doc) return;
    const o = state.over, s = sc.s, d = sc.doc, t = theme();
    for (const k of ["m20", "m50", "m200"]) s[k].applyOptions({ visible: o.ma });
    s.stu.applyOptions({ visible: o.st }); s.std.applyOptions({ visible: o.st });
    s.bbu.applyOptions({ visible: o.bb }); s.bbl.applyOptions({ visible: o.bb });
    for (const b of document.querySelectorAll("#st-over .tog")) b.setAttribute("aria-pressed", String(!!o[b.dataset.o]));
    const short = { brk55: "BO", hi52: "52H", pp: "PP", sqz: "SQ", stup: "ST", gc: "GC", tt: "TT", s2: "S2", pb: "PB", vcp: "VCP", macd: "M", vol: "V", brkdn: "BD", lo52: "52L", stdn: "ST", dc: "DC", s4: "S4", dist: "D", ob: "OB" };
    // Only setups with a measured edge on NEPSE are drawn (the side panel lists all of them).
    const marks = o.ev ? d.ev.filter(([, code]) => S.events[code] && ["up", "down"].includes(S.events[code].verdict))
      .sort((a, b) => a[0] - b[0]).map(([i, code]) => {
        const up = S.events[code].verdict === "up";
        return { time: d.d[i], position: up ? "belowBar" : "aboveBar", shape: up ? "arrowUp" : "arrowDown", color: up ? t.up : t.down, size: 1, text: short[code] };
      }) : [];
    s.candles.setMarkers(marks);
    for (const l of sc.srLines) s.candles.removePriceLine(l);
    sc.srLines = [];
    if (o.sr) {
      for (const x of d.res) sc.srLines.push(s.candles.createPriceLine({ price: x.p, color: t.down, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: `R ×${x.n}` }));
      for (const x of d.sup) sc.srLines.push(s.candles.createPriceLine({ price: x.p, color: t.up, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: `S ×${x.n}` }));
    }
  }
  function setLower(mode) {
    state.lower = mode;
    const s = sc.s;
    s.vol.applyOptions({ visible: mode === "vol" });
    s.rsi.applyOptions({ visible: mode === "rsi" });
    for (const k of ["mh", "macd", "sig"]) s[k].applyOptions({ visible: mode === "macd" });
    s.ai.applyOptions({ visible: mode === "ai" });
    s.rel.applyOptions({ visible: mode === "rel" });
    for (const b of document.querySelectorAll("#st-lower button")) b.setAttribute("aria-pressed", String(b.dataset.l === mode));
    stockLegend(null);
  }
  function setRange(n) {
    state.range = n;
    const len = sc.doc.d.length;
    sc.c1.timeScale().setVisibleLogicalRange({ from: Math.max(0, len - n) - 0.5, to: len - 1 + 3 });
    for (const b of document.querySelectorAll("#st-range button")) b.setAttribute("aria-pressed", String(Number(b.dataset.r) === n));
  }
  function stockLegend(i) {
    if (!sc || !sc.doc) return;
    const d = sc.doc, t = theme(), n = d.d.length;
    const k = i == null || i < 0 || i >= n ? n - 1 : i;
    const chg = k ? (d.c[k] / d.c[k - 1] - 1) * 100 : null;
    const sw = (c, dash) => `<span class="sw sw-line" style="background:${dash ? `repeating-linear-gradient(90deg,${c} 0 4px,transparent 4px 7px)` : c}"></span>`;
    let h = `<span class="item"><b>${esc(d.s)}</b></span><span class="item">${d.d[k]}</span>` +
      `<span class="item opt">O ${fmt(d.o[k])}</span><span class="item opt">H ${fmt(d.h[k])}</span><span class="item opt">L ${fmt(d.l[k])}</span>` +
      `<span class="item">C <b>${fmt(d.c[k])}</b></span><span class="item ${cls(chg)}">${pct(chg, 2)}</span>`;
    if (state.over.ma) h += `<span class="item">${sw(t.ma[0])}20 ${fmt(d.m20[k])}</span><span class="item">${sw(t.ma[1])}50 ${fmt(d.m50[k])}</span><span class="item">${sw(t.ma[2], 1)}200 ${fmt(d.m200[k])}</span>`;
    if (state.over.st && fin(d.st[k])) h += `<span class="item">${sw(d.sd[k] === 1 ? t.up : t.down)}Supertrend ${fmt(d.st[k])}</span>`;
    $("st-lg1").innerHTML = h;
    const L = {
      vol: `<b>Turnover</b> ${lakh(d.v[k])}`,
      rsi: `<b>RSI 14</b> ${fin(d.rsi[k]) ? d.rsi[k].toFixed(0) : "–"} <span class="opt">· above 70 overbought, below 30 oversold (on NEPSE, overbought tended to keep going)</span>`,
      macd: `<b>MACD 12/26/9</b> ${sw(t.accent)}${fin(d.macd[k]) ? d.macd[k].toFixed(2) : "–"} ${sw(t.ma[1])}signal ${fin(d.sig[k]) ? d.sig[k].toFixed(2) : "–"}`,
      ai: `<b>AI score</b> ${d.ai[k] ?? "–"} <span class="opt">· out-of-sample: each day scored by a model that had not seen it</span>`,
      rel: `<b>vs NEPSE</b> ${fin(d.rel[k]) ? d.rel[k].toFixed(1) : "–"} <span class="opt">· price ÷ index, 100 at the left edge; rising = beating the index</span>`,
    };
    $("st-lg2").innerHTML = `<span class="item">${L[state.lower]}</span>`;
  }
  async function openStock(sym) {
    if (!sym) return;
    sym = sym.toUpperCase();
    const st = bySym[sym];
    if (!st) { $("st-head").innerHTML = `<p class="quiet">No stock called ${esc(sym)} in the screener (mutual funds and debentures are left out).</p>`; return; }
    state.sym = sym;
    $("find-q").value = "";
    renderStockHead(st, null);
    let d;
    try { d = await loadDetail(sym); } catch (e) { $("st-scores").innerHTML = `<p class="quiet">Could not load ${esc(sym)}: ${esc(e.message)}</p>`; return; }
    if (state.sym !== sym) return;
    if (!sc) makeStockCharts();
    sc.doc = d;
    setStockData(d);
    setRange(state.range);
    renderStockHead(st, d);
    renderStockSide(st, d);
  }
  function renderStockHead(st, d) {
    $("st-head").innerHTML =
      `<div class="sh-l">${star(st.s)}<h1>${esc(st.s)}</h1><span class="secn">${esc(st.sec)}</span></div>` +
      `<div class="sh-p"><b>${fmt(st.c)}</b><span class="${cls(st.ch1)}">${arrow(st.ch1)}${pct(st.ch1, 2)}</span>` +
      `<span class="m">1W ${pct(st.ch5)} · 1M ${pct(st.ch20)} · 1Y ${pct(st.ch250)}</span></div>` +
      `<a class="more" href="#v=market&s=${encodeURIComponent(st.s)}" data-market="${esc(st.s)}">Full history in the breadth chart →</a>`;
    const tile = (k, v, sub, tipText) => `<div class="sct"${tipText ? ` data-tip="${tipText}"` : ""}><span class="k">${k}</span><span class="v">${v}</span><span class="d">${sub}</span></div>`;
    const stopPct = d && fin(d.stop) ? (d.stop / st.c - 1) * 100 : null;
    $("st-scores").innerHTML =
      `<div class="sct ai-t">${ring(st.ai, 64)}<div><span class="k">AI score</span><span class="d">${!fin(st.ai) ? "not scored (thin trading)" : st.ai >= 50 ? `ranked above ${st.ai - 1}% of stocks today` : `ranked below ${99 - st.ai}% of stocks today`}</span></div></div>` +
      tile("Trend", trendWord(st.tr), trendBar(st.tr) + ` ${signed(st.tr, 0)}`, "Votes of 7 moving averages and 5 oscillators, −100 to +100. Describes the chart; on NEPSE it did not predict returns by itself.") +
      tile("Relative strength", st.rs ?? "–", "IBD-style, 1–99", "40% last quarter + 20% each of the 3 before, ranked across all stocks") +
      tile("Trend template", (st.tt ?? "–") + "<small>/8</small>", "Minervini rules met", "") +
      tile("Weinstein stage", STAGES[st.stg] || "–", "30-week average", "") +
      tile("Supertrend", st.sd === 1 ? "▲ Up" : "▼ Down", st.sd === 1 && fin(stopPct) ? `trailing stop ${fmt(d.stop)} (${pct(stopPct)})` : "10-day ATR × 3", "") +
      tile("RSI · ADX", `${st.rsi ?? "–"} · ${st.adx ?? "–"}`, "momentum · trend strength", "");
  }
  function renderStockSide(st, d) {
    const w = d.why || { up: [], dn: [] };
    $("st-ai").innerHTML = `<h3>Why the AI scores it ${st.ai ?? "–"}</h3>` +
      (fin(st.ai) ? `<div class="whys"><div><span class="wk pos">▲ Pushing it up</span><ul class="why">${why(w.up) || "<li class=quiet>nothing stands out</li>"}</ul></div>` +
        `<div><span class="wk neg">▼ Holding it back</span><ul class="why">${why(w.dn) || "<li class=quiet>nothing stands out</li>"}</ul></div></div>`
        : `<p class="quiet">Too thinly traded or too new for the model.</p>`) +
      `<p class="note">The model's strongest inputs, with where this stock ranks today. <a href="#v=evidence">How accurate is it?</a></p>`;
    const recent = d.ev.filter(([i]) => i >= d.d.length - 60).sort((a, b) => b[0] - a[0]).slice(0, 8);
    $("st-ev").innerHTML = `<h3>Setups, last 3 months</h3>` + (recent.length
      ? `<ul class="evl">${recent.map(([i, c]) => `<li><span class="dt">${d.d[i].slice(5)}</span>${evChip(c, 0)}</li>`).join("")}</ul>`
      : `<p class="quiet">None in the last 60 sessions.</p>`);
    $("st-tt").innerHTML = `<h3>Trend template <small>${d.tt.filter(Boolean).length}/8</small></h3><ul class="ttl">` +
      S.tt_rules.map((r, k) => `<li class="${d.tt[k] ? "ok" : "no"}"><span aria-hidden="true">${d.tt[k] ? "✓" : "✗"}</span>${esc(r)}<span class="sr">${d.tt[k] ? "met" : "not met"}</span></li>`).join("") + `</ul>` +
      `<p class="note">On NEPSE the template alone did not predict returns; read it as "is this a healthy uptrend?".</p>`;
    const lv = (x) => `<tr><td>${fmt(x.p)}</td><td class="${cls(x.p - st.c)}">${pct((x.p / st.c - 1) * 100)}</td><td>${x.n} touch${x.n > 1 ? "es" : ""}</td></tr>`;
    $("st-lv").innerHTML = `<h3>Levels</h3><table class="lvt"><tbody>` +
      (d.res.length ? `<tr><th colspan="3">Resistance (auto, from swing highs)</th></tr>` + d.res.slice().reverse().map(lv).join("") : "") +
      `<tr class="now"><td>${fmt(st.c)}</td><td>now</td><td></td></tr>` +
      (d.sup.length ? `<tr><th colspan="3">Support (auto, from swing lows)</th></tr>` + d.sup.map(lv).join("") : "") +
      `</tbody></table><p class="note">52-week range ${fmt(d.lo52)} – ${fmt(d.hi52)} · daily range (ATR) ${fmt(d.atr)} = ${pct((d.atr / st.c) * 100, 1).replace("+", "")} of price</p>`;
  }

  // ---------------------------------------------------------------- sectors
  const QUAD = { Leading: "up", Weakening: "warn", Lagging: "down", Improving: "accent" };
  const SHORT = { "Commercial Banks": "Banks", "Development Banks": "Dev banks", "Finance": "Finance", "Hotels And Tourism": "Hotels", "Hydro Power": "Hydro",
    "Investment": "Investment", "Life Insurance": "Life ins.", "Manufacturing And Processing": "Manufacturing", "Microfinance": "Microfinance",
    "Non Life Insurance": "Non-life ins.", "Others": "Others", "Trading": "Trading" };
  let rrgHover = null;
  function renderSectors() {
    const t = theme(), R = S.rrg, el = $("rrg");
    $("rrg-sub").textContent = `vs the average stock · weekly, ${R.weeks[0]} → ${R.weeks[R.weeks.length - 1]}`;
    const W = el.clientWidth || 600, H = Math.min(520, Math.max(360, W * 0.55));
    const pts = Object.values(R.sectors).flat();
    const hx = Math.max(0.5, ...pts.map(([x]) => Math.abs(x - 100))) * 1.15;
    const hy = Math.max(0.3, ...pts.map(([, y]) => Math.abs(y - 100))) * 1.15;
    const pad = { l: 44, r: 12, t: 12, b: 28 };
    const X = (x) => pad.l + ((x - (100 - hx)) / (2 * hx)) * (W - pad.l - pad.r);
    const Y = (y) => pad.t + (1 - (y - (100 - hy)) / (2 * hy)) * (H - pad.t - pad.b);
    const cx = X(100), cy = Y(100);
    const q = (x, y, w, h, name, col, ax, ay, anchor) =>
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${t[col]}" fill-opacity="0.07"/><text class="ql" x="${ax}" y="${ay}" text-anchor="${anchor}" fill="${t.muted}">${name}</text>`;
    let svg = q(cx, pad.t, W - pad.r - cx, cy - pad.t, "LEADING", "up", W - pad.r - 8, pad.t + 16, "end") +
      q(cx, cy, W - pad.r - cx, H - pad.b - cy, "WEAKENING", "warn", W - pad.r - 8, H - pad.b - 8, "end") +
      q(pad.l, cy, cx - pad.l, H - pad.b - cy, "LAGGING", "down", pad.l + 8, H - pad.b - 8, "start") +
      q(pad.l, pad.t, cx - pad.l, cy - pad.t, "IMPROVING", "accent", pad.l + 8, pad.t + 16, "start");
    svg += `<line x1="${cx}" x2="${cx}" y1="${pad.t}" y2="${H - pad.b}" stroke="${t.border}"/><line x1="${pad.l}" x2="${W - pad.r}" y1="${cy}" y2="${cy}" stroke="${t.border}"/>`;
    const nice = (h) => [0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10].find((st) => h / st <= 4) || 10;
    const sx = nice(hx), sy = nice(hy), dg = (st) => (st < 1 ? (st < 0.25 ? 1 : 2) : 0);
    for (let v = 100 - Math.floor(hx / sx) * sx; v <= 100 + hx + 1e-9; v += sx)
      svg += `<text class="tk" x="${X(v)}" y="${H - pad.b + 16}" text-anchor="middle">${v.toFixed(dg(sx))}</text>`;
    for (let v = 100 - Math.floor(hy / sy) * sy; v <= 100 + hy + 1e-9; v += sy)
      svg += `<text class="tk" x="${pad.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v.toFixed(dg(sy))}</text>`;
    svg += `<text class="ax" x="${W - pad.r}" y="${H - 4}" text-anchor="end">RS-Ratio →</text><text class="ax" x="${pad.l + 4}" y="${pad.t - 2}" transform="rotate(0)">↑ RS-Momentum</text>`;
    const labels = [];
    for (const [name, tail] of Object.entries(R.sectors)) {
      const head = tail[tail.length - 1], qd = quadOf(head), col = t[QUAD[qd]];
      const hot = rrgHover === name, dim = rrgHover && !hot;
      svg += `<g class="rg-s${dim ? " dim" : ""}" data-sec="${esc(name)}">` +
        `<polyline points="${tail.map(([x, y]) => `${X(x).toFixed(1)},${Y(y).toFixed(1)}`).join(" ")}" fill="none" stroke="${hot ? t.ink : t.muted}" stroke-width="${hot ? 2 : 1.5}" stroke-linejoin="round" stroke-linecap="round"/>` +
        tail.slice(0, -1).map(([x, y], k) => `<circle cx="${X(x).toFixed(1)}" cy="${Y(y).toFixed(1)}" r="2.5" fill="${t.muted}" fill-opacity="${0.25 + (0.6 * k) / tail.length}"/>`).join("") +
        `<circle cx="${X(head[0]).toFixed(1)}" cy="${Y(head[1]).toFixed(1)}" r="6" fill="${col}" stroke="${t.surface}" stroke-width="2"/>` +
        `<circle class="hit" cx="${X(head[0]).toFixed(1)}" cy="${Y(head[1]).toFixed(1)}" r="14" fill="transparent" ` +
        `data-tip="<b>${esc(name)}</b> · ${qd}<br>RS-Ratio ${head[0].toFixed(1)} · RS-Momentum ${head[1].toFixed(1)}<br>1M ${pct((S.sectors.find((s) => s.name === name) || {}).ch20)} (equal-weight)"/></g>`;
      labels.push({ name, x: X(head[0]) + 9, y: Y(head[1]) + 4 });
    }
    // nudge colliding labels apart vertically (a few relaxation passes)
    for (let pass = 0; pass < 40; pass++) {
      let moved = false;
      for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
        const a = labels[i], b = labels[j];
        if (Math.abs(a.x - b.x) < 84 && Math.abs(a.y - b.y) < 13) {
          const push = (13 - Math.abs(a.y - b.y)) / 2 + 0.5, dir = a.y <= b.y ? -1 : 1;
          a.y += dir * push; b.y -= dir * push; moved = true;
        }
      }
      if (!moved) break;
    }
    for (const l of labels) {
      const right = l.x + 80 > W;
      svg += `<text class="lb" x="${right ? l.x - 18 : l.x}" y="${l.y}" text-anchor="${right ? "end" : "start"}">${esc(SHORT[l.name] || l.name)}</text>`;
    }
    el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Relative rotation graph of NEPSE sectors">${svg}</svg>`;

    const rows = S.sectors.slice().sort((a, b) => (b.ch20 ?? -99) - (a.ch20 ?? -99));
    const heat = (v, m) => (fin(v) ? ` style="background:${divColor(v, m, t)}33"` : "");
    $("sec-t").innerHTML = `<thead><tr><th>Sector</th><th>Stocks</th><th>1D</th><th>1W</th><th>1M</th><th>3M</th><th data-tip="Share of the sector's stocks with a trend rating of +15 or more">Uptrend</th><th data-tip="Average AI score of the sector's stocks">AI avg</th><th>Rotation</th></tr></thead><tbody>` +
      rows.map((r) => `<tr data-sec="${esc(r.name)}" tabindex="0"><td><b>${esc(r.name)}</b></td><td>${r.n}</td>` +
        [["ch1", 2, 2], ["ch5", 1, 5], ["ch20", 1, 10], ["ch60", 1, 20]].map(([k, dg, m]) => `<td class="${cls(r[k])}"${heat(r[k], m)}>${pct(r[k], dg)}</td>`).join("") +
        `<td>${r.up ?? "–"}%</td><td>${r.ai ?? "–"}</td>` +
        `<td>${r.q ? `<span class="qd q-${QUAD[r.q]}">${r.q}</span>${r.q_prev && r.q_prev !== r.q ? `<small> from ${r.q_prev}</small>` : ""}` : "–"}</td></tr>`).join("") + `</tbody>`;
  }
  const quadOf = ([x, y]) => (x >= 100 && y >= 100 ? "Leading" : x >= 100 ? "Weakening" : y < 100 ? "Lagging" : "Improving");

  // ---------------------------------------------------------------- evidence
  let portChart = null, portObs = null;
  function vbars(vals, labels, opts = {}) {
    // vertical diverging columns with signed labels at the tips
    const t = theme(), W = opts.w || 520, H = opts.h || 220, pad = { l: 8, r: 8, t: 22, b: 34 };
    const m = Math.max(...vals.map((v) => Math.abs(v || 0)), 0.01) * 1.15;
    const zero = pad.t + (H - pad.t - pad.b) / 2 * (opts.posOnly ? 2 : 1);
    const scale = (H - pad.t - pad.b) / (opts.posOnly ? m : 2 * m);
    const bw = Math.min(24, ((W - pad.l - pad.r) / vals.length) * 0.6), gap = (W - pad.l - pad.r) / vals.length;
    let s = `<line x1="${pad.l}" x2="${W - pad.r}" y1="${zero}" y2="${zero}" stroke="${t.border}"/>`;
    vals.forEach((v, i) => {
      if (!fin(v)) return;
      const x = pad.l + gap * i + (gap - bw) / 2, h = Math.abs(v) * scale, y = v >= 0 ? zero - h : zero;
      const col = opts.color ? opts.color(v, i, t) : v >= 0 ? t.up : t.down;
      const r = Math.min(4, h);
      const path = v >= 0
        ? `M${x},${zero} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${zero} Z`
        : `M${x},${zero} V${y + h - r} Q${x},${y + h} ${x + r},${y + h} H${x + bw - r} Q${x + bw},${y + h} ${x + bw},${y + h - r} V${zero} Z`;
      s += `<path d="${path}" fill="${col}"/>`;
      s += `<text class="bl" x="${x + bw / 2}" y="${v >= 0 ? y - 5 : y + h + 13}" text-anchor="middle">${opts.fmt ? opts.fmt(v) : signed(v, 2)}</text>`;
      s += `<text class="tk" x="${x + bw / 2}" y="${H - pad.b + 16}" text-anchor="middle">${esc(labels[i])}</text>`;
      s += `<rect x="${pad.l + gap * i}" y="${pad.t}" width="${gap}" height="${H - pad.t - pad.b}" fill="transparent" data-tip="${esc(opts.tip ? opts.tip(v, i) : labels[i] + ": " + signed(v, 2))}"/>`;
    });
    if (opts.foot) s += `<text class="tk" x="${pad.l}" y="${H - 4}">${esc(opts.foot[0])}</text><text class="tk" x="${W - pad.r}" y="${H - 4}" text-anchor="end">${esc(opts.foot[1])}</text>`;
    return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(opts.label || "")}">${s}</svg>`;
  }
  function hbars(items, opts = {}) {
    // horizontal bars around zero; items: {label, v, note, hi}
    const t = theme();
    const m = Math.max(...items.map((x) => Math.abs(x.v || 0)), 0.001);
    const neg = items.some((x) => x.v < 0);
    return `<div class="hb${neg ? " dv" : ""}">` + items.map((x) => {
      const w = (Math.abs(x.v || 0) / m) * (neg ? 50 : 100);
      const col = x.hi ? t.accent : opts.color ? opts.color(x) : t.muted;
      const side = neg ? (x.v >= 0 ? `left:50%` : `right:50%`) : `left:0`;
      return `<div class="hb-r" data-tip="${esc(x.tip || x.label)}"><span class="hb-l${x.hi ? " hi" : ""}">${esc(x.label)}</span>` +
        `<span class="hb-t">${neg ? `<span class="hb-z"></span>` : ""}<i style="${side};width:${w}%;background:${col}"></i></span>` +
        `<span class="hb-v">${opts.fmt ? opts.fmt(x.v) : signed(x.v, 3)}</span>${x.note != null ? `<span class="hb-n">${esc(x.note)}</span>` : ""}</div>`;
    }).join("") + `</div>`;
  }
  function renderEvidence() {
    const m = S.model, t = theme(), last = m.ic_by_year.length;
    const pos = m.ic_by_year.filter(([, v]) => v > 0).length;
    const dec = m.deciles;
    $("ev-hero").innerHTML =
      `<div class="hero-n"><span class="v">${m.ic.toFixed(3)}</span><span class="k">out-of-sample rank correlation<br>positive in ${pos} of ${last} years (${m.first_test_year}–${S.last_date.slice(0, 4)})</span></div>` +
      `<div class="hero-t"><p><b>What the AI does.</b> After each trading day a gradient-boosted tree model ranks every liquid stock by how much it is expected to beat the average stock over the next ${m.horizon} sessions, using ${m.n_features} readings at once.</p>` +
      `<p><b>How it was tested.</b> Each year since ${m.first_test_year} was scored by a model trained only on earlier years. The best-rated 10% then beat the average stock by <b class="pos">${pct(dec[9], 2)}</b> per ${m.horizon} sessions; the worst-rated 10% lagged by <b class="neg">${pct(dec[0], 2)}</b>.</p>` +
      `<p><b>Use it as a filter.</b> It is better at spotting stocks to avoid than at picking winners, and frequent trading of its top 10 lost the edge to costs (below).</p></div>`;
    $("ev-dec").innerHTML = `<h3>Return vs the average stock, by AI decile <small>${m.first_test_year}–now, out-of-sample</small></h3>` +
      vbars(dec, dec.map((_, i) => String(i + 1)), { label: "Average 20-session excess return by AI decile", foot: ["← lowest rated", "highest rated →"], fmt: (v) => signed(v, 2), tip: (v, i) => `Decile ${i + 1}: ${pct(v, 2)} vs the average stock per ${m.horizon} sessions` }) +
      `<p class="note">% per ${m.horizon} sessions, bought at the next open. Each bar is one tenth of the stocks, ranked by that day's AI score.</p>`;
    $("ev-methods").innerHTML = `<h3>Which well-known methods ranked NEPSE stocks? <small>${m.first_test_year}–now</small></h3>` +
      hbars(m.methods.map((x, i) => ({ label: x.name, v: x.ic, hi: i === 0, note: `${x.pos_years}/${x.years} yrs`, tip: `${x.name}: rank correlation ${signed(x.ic, 3)} with the next ${m.horizon} sessions' excess return; positive in ${x.pos_years} of ${x.years} years` })), { fmt: (v) => signed(v, 3) }) +
      `<p class="note">Rank correlation between each reading today and the next ${m.horizon} sessions' return vs the average stock (0 = chance, textbook settings, nothing fitted). Trend and momentum rules that work elsewhere were near zero on NEPSE.</p>`;
    $("ev-years").innerHTML = `<h3>AI accuracy by year</h3>` +
      vbars(m.ic_by_year.map(([, v]) => v), m.ic_by_year.map(([y]) => String(y)), { label: "AI rank correlation by year", fmt: (v) => v.toFixed(3), color: (v, i, th) => (v >= 0 ? th.accent : th.down), tip: (v, i) => `${m.ic_by_year[i][0]}: ${v.toFixed(3)}` }) +
      `<p class="note">Rank correlation per test year; ${m.ic_by_year[last - 1][0]} is the year so far.</p>`;
    $("ev-drivers").innerHTML = `<h3>What the AI leans on</h3>` +
      hbars(m.drivers.map(([lab, v]) => ({ label: lab, v, tip: `${lab}: ${v}% of the model's measured importance` })), { fmt: (v) => v.toFixed(0) + "%", color: () => t.accent }) +
      `<p class="note">Share of importance (permutation test, latest year). Calm, steadily rising stocks in strong sectors score best; volatile spikes score worst.</p>`;
    const P = m.portfolio;
    $("ev-port-head").innerHTML = `<h3>Could you have traded it? <small>every ${m.horizon} sessions, ${P.periods} rebalances, out-of-sample</small></h3>` +
      `<div class="stat-row"><div><span class="k">AI top 10, after ${P.cost}% costs</span><span class="v">${pct(P.cagr.top, 1)}<small>/yr</small></span><span class="d">${pct(P.cagr.top_gross, 1)} before costs · ${P.turnover}% replaced each time</span></div>` +
      `<div><span class="k">Average stock</span><span class="v">${pct(P.cagr.mkt, 1)}<small>/yr</small></span><span class="d">equal weight, no costs</span></div>` +
      `<div><span class="k">AI bottom 10</span><span class="v">${pct(P.cagr.bot, 1)}<small>/yr</small></span><span class="d">what the review list avoided</span></div></div>`;
    $("ev-port-lg").innerHTML = [["AI top 10 (after costs)", t.accent], ["Average stock", t.ink2], ["AI bottom 10", t.down]]
      .map(([n, c]) => `<span class="item"><span class="sw sw-line" style="background:${c}"></span>${n}</span>`).join("") + `<span class="item opt">growth of 100, log scale</span>`;
    const el = $("c-port");
    if (!portChart) {
      portChart = LC.createChart(el, Object.assign(chartOpts(t, true), { rightPriceScale: { borderColor: t.border, mode: LC.PriceScaleMode.Logarithmic } }));
      const so = (color, w) => ({ color, lineWidth: w, priceLineVisible: false, lastValueVisible: true, priceFormat: { type: "price", precision: 0, minMove: 1 } });
      for (const [k, c, w] of [["mkt", t.ink2, 1.5], ["bot", t.down, 1.5], ["top", t.accent, 2]]) portChart.addLineSeries(so(c, w)).setData(P.dates.map((time, i) => ({ time, value: P[k][i] })));
      if (!portObs) { portObs = new ResizeObserver(() => portChart && portChart.resize(el.clientWidth, el.clientHeight)); portObs.observe(el); }
    }
    portChart.resize(el.clientWidth, el.clientHeight);
    portChart.timeScale().fitContent();

    const h = m.halves, bh = m.base_hit;
    const order = { up: 0, none: 1, thin: 2, down: 3 };
    const evs = Object.entries(S.events).sort((a, b) => order[a[1].verdict] - order[b[1].verdict] || (b[1].h2.ex ?? -9) - (a[1].h2.ex ?? -9));
    const cell = (x) => (x.ex == null ? "<td>–</td><td></td>" : `<td class="${cls(x.ex)}">${pct(x.ex, 2)}</td><td class="sm">${x.hit}% · n ${nf0.format(x.n)}</td>`);
    $("ev-setups").innerHTML = `<h3>Textbook setups, measured on NEPSE <small>20-session return vs the average stock, from the next open</small></h3>` +
      `<div class="table-wrap flat"><table class="evt"><thead><tr><th>Setup</th><th>Textbook</th><th>On NEPSE</th><th>${h.h1}</th><th>beat avg</th><th>${h.h2}</th><th>beat avg</th></tr></thead><tbody>` +
      evs.map(([, e]) => `<tr><td data-tip="${esc(e.desc)}"><b>${esc(e.label)}</b></td><td>${e.textbook > 0 ? "bullish" : "bearish"}</td>` +
        `<td><span class="vb v-${e.verdict}">${VERDICT[e.verdict][0]}</span> ${VERDICT[e.verdict][1]}</td>${cell(e.h1)}${cell(e.h2)}</tr>`).join("") +
      `</tbody></table></div><p class="note">A setup "works" only if both periods agree (±0.25% or more). A random stock beat the average ${bh.h1}% / ${bh.h2}% of the time: most stocks lag while a few soar, so under 50% is normal.</p>`;
    const L = m.live;
    $("ev-live").innerHTML = `<h3>Live record</h3>` + (L.complete_days
      ? `<div class="stat-row"><div><span class="k">AI top-rated</span><span class="v ${cls(L.pick.ex)}">${pct(L.pick.ex, 2)}</span><span class="d">vs avg, beat it on ${L.pick.beat}% of days</span></div>` +
        `<div><span class="k">Review list</span><span class="v ${cls(L.avoid.ex)}">${pct(L.avoid.ex, 2)}</span><span class="d">vs avg, beat it on ${L.avoid.beat}% of days</span></div></div>` +
        `<p class="note">${L.complete_days} of ${L.days} logged days have a full ${m.horizon}-session outcome (logging since ${esc(L.since)}).</p>`
      : `<p>Every trading day's suggestions are saved to <code>data/picks-log.csv</code> ${L.days ? `(since ${esc(L.since)}, ${L.days} day${L.days > 1 ? "s" : ""} so far)` : "starting with the next update"} and scored here once ${m.horizon} sessions have passed. Unlike the backtest, nobody can tune this record after the fact.</p>`);
    $("ev-how").innerHTML = `<h3>How it works</h3><ul class="how">` +
      `<li><b>Data:</b> daily prices of every NEPSE stock, adjusted for bonus and rights issues; real opens and highs/lows exist from 2018, so studies start mid-2018.</li>` +
      `<li><b>Readings:</b> ${m.n_features} technical readings per stock, each turned into a 0–1 rank within the day so 2018 and 2026 compare fairly.</li>` +
      `<li><b>Target:</b> return from the next open over ${m.horizon} sessions, minus the average stock's.</li>` +
      `<li><b>Model:</b> scikit-learn gradient-boosted trees, textbook settings, retrained every day on data up to ${esc(m.trained_through)} (${nf0.format(m.train_rows)} rows).</li>` +
      `<li><b>Universe:</b> stocks with 120+ sessions of history, leaving out the least-traded 20%; suggestions also need ${S.pick_liq_min} lakh of daily turnover.</li>` +
      `<li><b>Limits:</b> it sees only prices and turnover, not earnings, dividends, rates or NRB policy. Past behaviour, not advice.</li></ul>`;
  }

  // ---------------------------------------------------------------- wiring
  function openSymFromEvent(e) {
    if (e.target.closest(".star")) return false;
    const el = e.target.closest("[data-sym]");
    if (!el) return false;
    go("stock", el.dataset.sym);
    return true;
  }
  function screenSector(name) {
    state.fsec = name; state.fq = ""; state.filters.clear();
    $("f-sec").value = name; $("f-q").value = "";
    go("screener");
  }
  function wire() {
    document.addEventListener("click", (e) => {
      const st = e.target.closest(".star");
      if (st) { e.preventDefault(); e.stopPropagation(); toggleWatch(st.dataset.star); return; }
      const nav = e.target.closest(".tabs a");
      if (nav) { e.preventDefault(); go(nav.dataset.view, nav.dataset.view === "stock" ? state.sym : null); return; }
      const mk = e.target.closest("[data-market]");
      if (mk) { e.preventDefault(); window.NepseMarket.openSymbol(mk.dataset.market); const p = new URLSearchParams(location.hash.slice(1)); p.set("v", "market"); p.set("s", mk.dataset.market); location.hash = "#" + p.toString(); return; }
      if (e.target.closest("#v-today, #v-screener tbody, #v-sectors .rrg")) { if (openSymFromEvent(e)) return; }
      const srt = e.target.closest("#scr button[data-sort]");
      if (srt) { const k = srt.dataset.sort; state.sort = { k, d: state.sort.k === k ? -state.sort.d : k === "s" ? 1 : -1 }; renderScreener(); return; }
      const sec = e.target.closest("#sec-t tr[data-sec], #rrg [data-sec]");
      if (sec) screenSector(sec.dataset.sec);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const el = e.target.closest("[data-sym][tabindex], #sec-t tr[data-sec]");
      if (!el) return;
      if (el.dataset.sec) screenSector(el.dataset.sec); else go("stock", el.dataset.sym);
    });
    $("rrg").addEventListener("pointerover", (e) => { const g = e.target.closest("[data-sec]"); const n = g ? g.dataset.sec : null; if (n !== rrgHover) { rrgHover = n; renderSectors(); } });
    $("rrg").addEventListener("pointerleave", () => { rrgHover = null; renderSectors(); });
    window.addEventListener("hashchange", route);
    $("find").addEventListener("submit", (e) => { e.preventDefault(); const s = $("find-q").value.trim().toUpperCase().split(" ")[0]; if (bySym[s]) go("stock", s); });
    $("find-q").addEventListener("change", () => { const s = $("find-q").value.trim().toUpperCase().split(" ")[0]; if (bySym[s]) go("stock", s); });
    for (const b of document.querySelectorAll("#map-win button")) b.addEventListener("click", () => { state.mapWin = b.dataset.w; renderMap(); });
    $("f-q").addEventListener("input", () => { state.fq = $("f-q").value.trim().toUpperCase(); renderScreener(); });
    $("f-sec").addEventListener("change", () => { state.fsec = $("f-sec").value; renderScreener(); });
    for (const b of document.querySelectorAll("#f-tags .tog")) b.addEventListener("click", () => { const f = b.dataset.f; if (state.filters.has(f)) state.filters.delete(f); else state.filters.add(f); renderScreener(); });
    for (const b of document.querySelectorAll("#st-over .tog")) b.addEventListener("click", () => { state.over[b.dataset.o] = !state.over[b.dataset.o]; applyOverlays(); stockLegend(null); });
    for (const b of document.querySelectorAll("#st-lower button")) b.addEventListener("click", () => sc && setLower(b.dataset.l));
    for (const b of document.querySelectorAll("#st-range button")) b.addEventListener("click", () => sc && setRange(Number(b.dataset.r)));
    let rz = null;
    window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (state.view === "today") renderMap(); if (state.view === "sectors") renderSectors(); }, 150); });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", () => {
      if (sc) { sc.ro.disconnect(); sc.c1.remove(); sc.c2.remove(); sc = null; }
      if (portChart) { portChart.remove(); portChart = null; }
      renderToday();
      if (state.view === "stock") openStock(state.sym);
      if (state.view === "sectors") renderSectors();
      if (state.view === "evidence") renderEvidence();
      if (state.view === "screener") renderScreener();
    });
  }

  async function main() {
    try {
      const res = await fetch("data/screener.json", { cache: "no-cache" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      S = await res.json();
    } catch (e) {
      // The breadth chart has its own data, so keep it usable without the screener.
      $("fresh").textContent = "";
      $("boot").innerHTML = `Today's stock analysis is not available (${esc(e.message)}). <a href="#v=market">Open the market breadth chart</a>.`;
      const fallback = () => {
        const market = readRoute().v === "market";
        $("v-market").hidden = !market;
        $("boot").hidden = market;
        if (market) window.NepseMarket.start();
      };
      window.addEventListener("hashchange", fallback);
      fallback();
      return;
    }
    for (const st of S.stocks) bySym[st.s] = st;
    $("find-list").innerHTML = S.stocks.map((s) => `<option value="${esc(s.s)}">${esc(s.sec)}</option>`).join("");
    $("f-sec").innerHTML = `<option value="">All sectors</option>` + [...new Set(S.stocks.map((s) => s.sec))].sort().map((s) => `<option>${esc(s)}</option>`).join("");
    renderFresh();
    const seen = computeSeen();
    wire();
    $("boot").hidden = true;
    route();
    renderToday();
    renderBanner(seen);
  }
  main();
})();
