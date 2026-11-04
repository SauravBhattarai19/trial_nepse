/* NEPSE market breadth - interactive chart.
 *
 * data/market.json (built by scripts/build_site.py) holds one shared date
 * axis, the NEPSE index OHLC and, per security type, daily advancer /
 * decliner / unchanged counts. Breadth and the AD line are recomputed here
 * whenever mutual funds or debentures are toggled.
 *
 * Three stacked Lightweight Charts share that date axis: every series gets a
 * point (or whitespace) on every date, so logical indexes line up and the
 * panes can be scrolled, zoomed and crosshaired together.
 */
(function () {
  "use strict";
  const LC = window.LightweightCharts;
  const $ = (id) => document.getElementById(id);

  const TYPE_LABEL = { equity: "Stock", mutual_fund: "Mutual fund", debenture: "Debenture" };
  const RANGE_MONTHS = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60 };

  const MA_PRESETS = [3, 5, 7, 10, 20, 50];
  const MA_DEFAULT = [5, 7];
  const MA_MAX = 3;          // at most 3 lines: no 4-colour set stays distinguishable in every combination
  const MA_MIN_DAYS = 2, MA_MAX_DAYS = 1000;

  const state = { mf: true, deb: true, symbol: "NEPSE", range: "1Y", mas: MA_DEFAULT.slice() };
  const maSlot = new Map();  // period -> colour slot 0..2; a line keeps its colour while it is on
  let data = null;          // market.json
  let dates = [];           // shared axis, "YYYY-MM-DD"
  let breadth = null;       // {a, d, u, net, ad, maCache}
  let price = null;         // {o, h, l, c, t, prev, label, kind, maCache}
  let typeOf = {};          // symbol -> type
  const companyCache = new Map();
  let charts, series;

  // ---------- state <-> URL hash ----------
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get("mf") === "0") state.mf = false;
    if (p.get("deb") === "0") state.deb = false;
    if (p.get("s")) state.symbol = p.get("s").toUpperCase();
    if (p.get("r") && (RANGE_MONTHS[p.get("r")] || p.get("r") === "ALL")) state.range = p.get("r");
    if (p.has("ma")) {
      const ns = p.get("ma").split(",").map(Number).filter(validDays);
      state.mas = [...new Set(ns)].slice(0, MA_MAX);
    }
  }
  const validDays = (n) => Number.isInteger(n) && n >= MA_MIN_DAYS && n <= MA_MAX_DAYS;
  function writeHash() {
    const p = new URLSearchParams();
    if (!state.mf) p.set("mf", "0");
    if (!state.deb) p.set("deb", "0");
    if (state.symbol !== "NEPSE") p.set("s", state.symbol);
    if (state.range !== "1Y") p.set("r", state.range);
    const mas = state.mas.slice().sort((x, y) => x - y).join(",");
    if (mas !== MA_DEFAULT.join(",")) p.set("ma", mas || "0");
    const h = p.toString().replace(/%2C/g, ",");
    history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
  }

  // ---------- formatting ----------
  const nf2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fmt = (v) => (v == null || !isFinite(v) ? "–" : nf2.format(v));
  const fmtInt = (v) => (v == null || !isFinite(v) ? "–" : nf0.format(v));
  const signed = (v, f = fmt) => (v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + f(Math.abs(v)));
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  function npr(v) { // Nepali units: lakh = 1e5, crore = 1e7, arba = 1e9
    if (v == null || !isFinite(v)) return "–";
    if (v >= 1e9) return nf2.format(v / 1e9) + " Arba";
    if (v >= 1e7) return nf2.format(v / 1e7) + " Cr";
    if (v >= 1e5) return nf2.format(v / 1e5) + " Lakh";
    return nf0.format(v);
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---------- math ----------
  function sma(values, n) {
    const out = new Array(values.length).fill(null);
    const win = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v == null) continue;          // skip non-trading days for this security
      win.push(v); sum += v;
      if (win.length > n) sum -= win.shift();
      if (win.length === n) out[i] = sum / n;
    }
    return out;
  }
  // moving average of `values`, computed once per period and cached on `owner`
  function maOf(owner, values, n) {
    if (!owner.maCache) owner.maCache = new Map();
    if (!owner.maCache.has(n)) owner.maCache.set(n, sma(values, n));
    return owner.maCache.get(n);
  }

  function computeBreadth() {
    const kinds = ["equity"];
    if (state.mf) kinds.push("mutual_fund");
    if (state.deb) kinds.push("debenture");
    const n = dates.length;
    const a = new Array(n).fill(0), d = new Array(n).fill(0), u = new Array(n).fill(0);
    for (const k of kinds) {
      const b = data.breadth[k];
      for (let i = 0; i < n; i++) { a[i] += b.a[i]; d[i] += b.d[i]; u[i] += b.u[i]; }
    }
    const net = a.map((x, i) => x - d[i]);
    const ad = [];
    let run = 0;
    for (const x of net) { run += x; ad.push(run); }
    breadth = { a, d, u, net, ad };
  }

  function priceFromIndex() {
    const ix = data.index;
    return finishPrice({ o: ix.o, h: ix.h, l: ix.l, c: ix.c, t: ix.t, label: "NEPSE Index", kind: null });
  }
  function finishPrice(p) {
    p.prev = new Array(p.c.length).fill(null);   // previous close, for the change readout
    let last = null;
    for (let i = 0; i < p.c.length; i++) { p.prev[i] = last; if (p.c[i] != null) last = p.c[i]; }
    return p;
  }

  async function loadCompany(symbol) {
    if (companyCache.has(symbol)) return companyCache.get(symbol);
    const res = await fetch("data/company-wise/" + encodeURIComponent(symbol) + ".csv");
    if (!res.ok) throw new Error("No data file for " + symbol);
    const lines = (await res.text()).trim().split(/\r?\n/);
    const head = lines.shift().split(",");
    const col = (name) => head.indexOf(name);
    const iD = col("published_date"), iO = col("open"), iH = col("high"), iL = col("low"), iC = col("close"), iT = col("traded_amount");
    const pos = new Map(dates.map((d, i) => [d, i]));
    const n = dates.length;
    const p = { o: new Array(n).fill(null), h: new Array(n).fill(null), l: new Array(n).fill(null), c: new Array(n).fill(null), t: new Array(n).fill(null) };
    const num = (s) => { const v = parseFloat(s); return isFinite(v) ? v : null; };
    for (const line of lines) {
      const f = line.split(",");
      const i = pos.get(f[iD]);
      if (i === undefined) continue;
      const c = num(f[iC]);
      if (c == null || c <= 0) continue;  // unlisted/suspended placeholder rows
      p.c[i] = c;
      p.o[i] = num(f[iO]) || c; p.h[i] = num(f[iH]) || c; p.l[i] = num(f[iL]) || c;
      p.t[i] = num(f[iT]);
    }
    p.label = symbol;
    p.kind = typeOf[symbol];
    finishPrice(p);
    companyCache.set(symbol, p);
    return p;
  }

  // ---------- charts ----------
  function theme() {
    const s = getComputedStyle(document.documentElement);
    const v = (n) => s.getPropertyValue(n).trim();
    return {
      surface: v("--surface"), grid: v("--grid"), border: v("--border"), muted: v("--muted"),
      ink: v("--ink"), up: v("--up"), down: v("--down"), ma: [v("--ma1"), v("--ma2"), v("--ma3")],
      ad: v("--ad"), vol: v("--vol"),
    };
  }

  function chartOptions(t, showTime, intAxis, logo) {
    return {
      layout: {
        attributionLogo: logo,
        background: { type: "solid", color: t.surface },
        textColor: t.muted,
        fontSize: 11,
        fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
      },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.border, minimumWidth: 84, scaleMargins: { top: 0.16, bottom: 0.08 } },
      timeScale: { borderColor: t.border, visible: showTime, rightOffset: 4, minBarSpacing: 0.02, timeVisible: false },
      crosshair: {
        mode: LC.CrosshairMode.Normal,
        vertLine: { color: t.muted, labelBackgroundColor: t.ink },
        horzLine: { color: t.muted, labelBackgroundColor: t.ink },
      },
      handleScroll: { vertTouchDrag: false },
      localization: { priceFormatter: intAxis ? (p) => nf0.format(p) : (p) => nf2.format(p) },
    };
  }

  function seriesOptions(t) {
    return {
      candles: {
        upColor: "rgba(0,0,0,0)", borderUpColor: t.up, wickUpColor: t.up,   // hollow up candles
        downColor: t.down, borderDownColor: t.down, wickDownColor: t.down,   // filled down candles
        priceLineVisible: true, priceLineColor: t.muted, lastValueVisible: true,
      },
      volume: { color: t.vol, priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
      ad: { color: t.ad, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, priceFormat: { type: "price", precision: 0, minMove: 1 } },
      adv: { color: t.up, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "price", precision: 0, minMove: 1 } },
      dec: { color: t.down, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "price", precision: 0, minMove: 1 } },
    };
  }

  function createCharts() {
    const t = theme();
    const so = seriesOptions(t);
    const make = (id, showTime, intAxis, logo) => {
      const el = $(id);
      const c = LC.createChart(el, chartOptions(t, showTime, intAxis, logo));
      c.resize(el.clientWidth, el.clientHeight);
      return c;
    };
    charts = {
      price: make("c-price", false, false, false),
      ad: make("c-ad", false, true, false),
      hist: make("c-hist", true, true, true),   // one TradingView attribution, bottom pane
    };
    // Size charts ourselves (not autoSize) so a resize can keep the visible
    // date range instead of LC's default of keeping the bar spacing.
    const ro = new ResizeObserver(() => {
      const r = charts.price.timeScale().getVisibleLogicalRange();
      for (const [name, c] of Object.entries(charts)) {
        const el = $("c-" + name);
        c.resize(el.clientWidth, el.clientHeight);
      }
      if (r) charts.price.timeScale().setVisibleLogicalRange(r);
    });
    ro.observe($("panes"));
    window.nepseCharts = charts; // handy for debugging from the console
    series = {
      volume: charts.price.addHistogramSeries(so.volume),
      candles: charts.price.addCandlestickSeries(so.candles),
      ad: charts.ad.addLineSeries(so.ad),
      adv: charts.hist.addHistogramSeries(so.adv),
      dec: charts.hist.addHistogramSeries(so.dec),
    };
    charts.price.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    charts.hist.priceScale("right").applyOptions({ scaleMargins: { top: 0.18, bottom: 0.04 } });

    const all = Object.values(charts);
    // keep the three time axes in lockstep
    let syncingRange = false;
    for (const c of all) {
      c.timeScale().subscribeVisibleLogicalRangeChange((r) => {
        if (syncingRange || !r) return;
        syncingRange = true;
        for (const o of all) if (o !== c) o.timeScale().setVisibleLogicalRange(r);
        syncingRange = false;
      });
    }
    // shared crosshair + legends
    const anchor = (name, i) => {
      if (name === "price") return [series.candles, nearest(price.c, i)];
      if (name === "ad") return [series.ad, breadth.ad[i]];
      return [series.adv, breadth.a[i]];
    };
    let syncingCross = false;
    for (const [name, c] of Object.entries(charts)) {
      c.subscribeCrosshairMove((p) => {
        if (syncingCross) return;
        syncingCross = true;
        const i = p && p.time !== undefined && p.logical != null ? Math.round(p.logical) : null;
        for (const [oname, o] of Object.entries(charts)) {
          if (o === c) continue;
          if (i == null || i < 0 || i >= dates.length) { o.clearCrosshairPosition(); continue; }
          const [s, v] = anchor(oname, i);
          if (v == null) o.clearCrosshairPosition();
          else o.setCrosshairPosition(v, dates[i], s);
        }
        syncingCross = false;
        renderLegends(i != null && i >= 0 && i < dates.length ? i : null);
      });
    }

    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(applyTheme);
  }

  function nearest(arr, i) { // last available value at or before i
    for (let j = i; j >= 0; j--) if (arr[j] != null) return arr[j];
    return null;
  }

  function applyTheme() {
    const t = theme();
    charts.price.applyOptions(chartOptions(t, false, false, false));
    charts.ad.applyOptions(chartOptions(t, false, true, false));
    charts.hist.applyOptions(chartOptions(t, true, true, true));
    const so = seriesOptions(t);
    for (const k of Object.keys(series)) series[k].applyOptions(so[k]);
    renderMas();
    renderTiles();
    renderLegends(null);
  }

  function lineData(values) {
    return dates.map((time, i) => (values[i] == null ? { time } : { time, value: values[i] }));
  }

  function setPriceData() {
    const p = price;
    series.candles.setData(dates.map((time, i) =>
      p.c[i] == null || p.o[i] == null || p.h[i] == null || p.l[i] == null
        ? { time }
        : { time, open: p.o[i], high: Math.max(p.h[i], p.o[i], p.c[i]), low: Math.min(p.l[i], p.o[i], p.c[i]), close: p.c[i] }));
    series.volume.setData(lineData(p.t));
  }

  function setBreadthData() {
    const b = breadth;
    series.ad.setData(lineData(b.ad));
    series.adv.setData(dates.map((time, i) => ({ time, value: b.a[i] })));
    series.dec.setData(dates.map((time, i) => ({ time, value: -b.d[i] })));
  }

  // ---------- moving averages ----------
  // One line on the price pane and one on the AD pane per active period.
  // Lines are rebuilt from scratch on any change (at most 6 series).
  let maSeries = [];
  const maStyle = (slot) => (slot === 2 ? LC.LineStyle.Dashed : LC.LineStyle.Solid);

  function assignSlots() {
    for (const n of [...maSlot.keys()]) if (!state.mas.includes(n)) maSlot.delete(n);
    for (const n of state.mas) {
      if (maSlot.has(n)) continue;
      const used = new Set(maSlot.values());
      maSlot.set(n, [0, 1, 2].find((s) => !used.has(s)));
    }
  }

  function renderMas() {
    assignSlots();
    for (const [chart, s] of maSeries) chart.removeSeries(s);
    maSeries = [];
    const t = theme();
    for (const n of state.mas) {
      const slot = maSlot.get(n);
      const opts = {
        color: t.ma[slot], lineWidth: 1.5, lineStyle: maStyle(slot),
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      };
      const ps = charts.price.addLineSeries(opts);
      ps.setData(lineData(maOf(price, price.c, n)));
      const as = charts.ad.addLineSeries(Object.assign({}, opts, { priceFormat: { type: "price", precision: 0, minMove: 1 } }));
      as.setData(lineData(maOf(breadth, breadth.ad, n)));
      maSeries.push([charts.price, ps], [charts.ad, as]);
    }
    renderMaControls();
  }

  function maSwatch(n) {
    const slot = maSlot.get(n);
    const c = theme().ma[slot];
    const bg = slot === 2 ? `repeating-linear-gradient(90deg, ${c} 0 4px, transparent 4px 7px)` : c;
    return `<span class="sw sw-line" style="background:${bg}"></span>`;
  }

  function renderMaControls() {
    const full = state.mas.length >= MA_MAX;
    const shown = [...new Set([...MA_PRESETS, ...state.mas])].sort((a, b) => a - b);
    $("ma-chips").innerHTML = shown.map((n) => {
      const on = state.mas.includes(n);
      return `<button type="button" class="ma" data-n="${n}" aria-pressed="${on}"` +
        (!on && full ? ` aria-disabled="true" title="Up to ${MA_MAX} at once"` : "") +
        `>${on ? maSwatch(n) : ""}${n}</button>`;
    }).join("");
  }

  function setMaMessage(text) { $("ma-msg").textContent = text || ""; }

  function toggleMa(n, forceOn) {
    const on = state.mas.includes(n);
    if (on && !forceOn) {
      state.mas = state.mas.filter((x) => x !== n);
    } else if (!on) {
      if (state.mas.length >= MA_MAX) {
        setMaMessage(`Up to ${MA_MAX} moving averages at once. Turn one off first.`);
        return;
      }
      state.mas = state.mas.concat(n);
    }
    setMaMessage("");
    renderMas();
    renderLegends(null);
    writeHash();
  }

  function applyRange() {
    const n = dates.length;
    let from = 0;
    if (state.range !== "ALL") {
      const end = new Date(dates[n - 1] + "T00:00:00Z");
      end.setUTCMonth(end.getUTCMonth() - RANGE_MONTHS[state.range]);
      const cutoff = end.toISOString().slice(0, 10);
      from = dates.findIndex((d) => d >= cutoff);
      if (from < 0) from = 0;
    }
    charts.price.timeScale().setVisibleLogicalRange({ from: from - 0.5, to: n - 1 + 4 });
    for (const b of document.querySelectorAll("#ranges button")) b.setAttribute("aria-pressed", String(b.dataset.range === state.range));
  }

  // ---------- readouts ----------
  function lastIndexWith(arr) { for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return i; return -1; }

  function renderLegends(i) {
    const t = theme();
    const p = price, b = breadth;
    const at = i == null ? lastIndexWith(p.c) : i;
    const bi = i == null ? dates.length - 1 : i;
    const sw = (color, line) => `<span class="sw${line ? " sw-line" : ""}" style="background:${color}"></span>`;
    const maItems = (value) => state.mas.slice().sort((x, y) => x - y)
      .map((n) => `<span class="item">${maSwatch(n)}MA${n} ${value(n)}</span>`).join("");

    let html = `<span class="item"><b>${esc(p.label)}</b>${p.kind ? `<span class="tag">${TYPE_LABEL[p.kind]}</span>` : ""}</span>`;
    if (at >= 0 && p.c[at] != null) {
      const chg = p.prev[at] ? ((p.c[at] - p.prev[at]) / p.prev[at]) * 100 : null;
      html += `<span class="item">${dates[at]}</span>` +
        `<span class="item opt">O ${fmt(p.o[at])}</span><span class="item opt">H ${fmt(p.h[at])}</span>` +
        `<span class="item opt">L ${fmt(p.l[at])}</span><span class="item">C <b>${fmt(p.c[at])}</b></span>` +
        `<span class="item ${cls(chg)}">${signed(chg)}%</span>` +
        maItems((n) => fmt(maOf(p, p.c, n)[at])) +
        `<span class="item opt">Turnover ${npr(p.t[at])}</span>`;
    } else if (i != null) {
      html += `<span class="item">${dates[i]} · no trade</span>`;
    }
    $("lg-price").innerHTML = html;

    $("lg-ad").innerHTML =
      `<span class="item"><b>Advance/Decline line</b></span>` +
      `<span class="item opt">${dates[bi]}</span>` +
      `<span class="item">${sw(t.ad, 1)}<b>${fmtInt(b.ad[bi])}</b> <span class="${cls(b.net[bi])}">(${signed(b.net[bi], fmtInt)})</span></span>` +
      maItems((n) => fmtInt(maOf(b, b.ad, n)[bi]));

    $("lg-hist").innerHTML =
      `<span class="item"><b>Daily breadth</b></span>` +
      `<span class="item">${sw(t.up)}Advancers ${fmtInt(b.a[bi])}</span>` +
      `<span class="item">${sw(t.down)}Decliners ${fmtInt(b.d[bi])}</span>` +
      `<span class="item opt">Unchanged ${fmtInt(b.u[bi])}</span>`;
  }

  function renderTiles() {
    const t = theme();
    const ix = data.index, b = breadth, n = dates.length;
    const li = lastIndexWith(ix.c);
    let pi = li - 1; while (pi >= 0 && ix.c[pi] == null) pi--;
    const chg = pi >= 0 ? ix.c[li] - ix.c[pi] : null;
    const pct = pi >= 0 ? (chg / ix.c[pi]) * 100 : null;
    const last = n - 1;
    const total = b.a[last] + b.d[last] + b.u[last];
    const share = (x) => (total ? Math.round((x / total) * 100) + "% of " + fmtInt(total) : "");
    const tile = (k, v, d) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
    $("tiles").innerHTML =
      tile(`NEPSE · ${dates[li]}`, fmt(ix.c[li]), `<span class="${cls(chg)}">${signed(chg)} (${signed(pct)}%)</span> · Turnover ${npr(ix.t[li])}`) +
      tile(`<span class="sw" style="background:${t.up}"></span>Advancers`, fmtInt(b.a[last]), share(b.a[last])) +
      tile(`<span class="sw" style="background:${t.down}"></span>Decliners`, fmtInt(b.d[last]), share(b.d[last])) +
      tile("Unchanged", fmtInt(b.u[last]), share(b.u[last])) +
      tile("AD line, 5-day change", signed(b.ad[last] - b.ad[Math.max(0, last - 5)], fmtInt), `Today ${signed(b.net[last], fmtInt)}`);
  }

  function renderTable() {
    const ix = data.index, b = breadth, n = dates.length;
    const rows = [];
    for (let i = n - 1; i >= Math.max(0, n - 10); i--) {
      let pi = i - 1; while (pi >= 0 && ix.c[pi] == null) pi--;
      const pct = ix.c[i] != null && pi >= 0 ? ((ix.c[i] - ix.c[pi]) / ix.c[pi]) * 100 : null;
      rows.push(`<tr><td>${dates[i]}</td><td>${fmt(ix.c[i])}</td><td class="${cls(pct)}">${pct == null ? "–" : signed(pct) + "%"}</td>` +
        `<td>${fmtInt(b.a[i])}</td><td>${fmtInt(b.d[i])}</td><td>${fmtInt(b.u[i])}</td><td class="${cls(b.net[i])}">${signed(b.net[i], fmtInt)}</td></tr>`);
    }
    document.querySelector("#recent-table tbody").innerHTML = rows.join("");
  }

  function renderBreadthAll(keepRange) {
    const r = keepRange ? charts.price.timeScale().getVisibleLogicalRange() : null;
    computeBreadth();
    setBreadthData();
    renderMas();
    if (r) charts.price.timeScale().setVisibleLogicalRange(r);
    renderTiles();
    renderTable();
    renderLegends(null);
    $("t-mf").setAttribute("aria-pressed", String(state.mf));
    $("t-deb").setAttribute("aria-pressed", String(state.deb));
  }

  async function selectSymbol(sym) {
    sym = (sym || "NEPSE").trim().toUpperCase();
    if (sym === "" || sym === "NEPSE" || sym.startsWith("NEPSE ")) sym = "NEPSE";
    if (sym !== "NEPSE" && !typeOf[sym]) { $("symbol").value = state.symbol; return; }
    const r = charts.price.timeScale().getVisibleLogicalRange();
    try {
      price = sym === "NEPSE" ? priceFromIndex() : await loadCompany(sym);
      state.symbol = sym;
    } catch (e) {
      console.error(e);
      $("symbol").value = state.symbol;
      return;
    }
    $("symbol").value = sym;
    setPriceData();
    renderMas();
    if (r) charts.price.timeScale().setVisibleLogicalRange(r);
    renderLegends(null);
    writeHash();
  }

  function downloadCsv() {
    const ix = data.index, b = breadth;
    const mas = state.mas.slice().sort((x, y) => x - y);
    const adMa = mas.map((n) => maOf(b, b.ad, n));
    const ixMa = mas.map((n) => sma(ix.c, n));
    const r = (v, k) => (v == null ? "" : +v.toFixed(k));
    const lines = [["date,advancers,decliners,unchanged,net_advances,ad_line", ...mas.map((n) => "ad_line_ma" + n),
      "nepse_close", ...mas.map((n) => "nepse_close_ma" + n)].join(",")];
    dates.forEach((d, i) => lines.push([d, b.a[i], b.d[i], b.u[i], b.net[i], b.ad[i], ...adMa.map((m) => r(m[i], 2)),
      r(ix.c[i], 2), ...ixMa.map((m) => r(m[i], 2))].join(",")));
    const parts = ["nepse-breadth"];
    if (!state.mf) parts.push("ex-mutual-funds");
    if (!state.deb) parts.push("ex-debentures");
    const url = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: parts.join("-") + ".csv" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function wireControls() {
    $("t-mf").addEventListener("click", () => { state.mf = !state.mf; renderBreadthAll(true); writeHash(); });
    $("t-deb").addEventListener("click", () => { state.deb = !state.deb; renderBreadthAll(true); writeHash(); });
    for (const b of document.querySelectorAll("#ranges button")) {
      b.addEventListener("click", () => { state.range = b.dataset.range; applyRange(); writeHash(); });
    }
    const input = $("symbol");
    input.addEventListener("change", () => selectSymbol(input.value.split(" ")[0]));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
    input.addEventListener("focus", () => input.select());
    $("download").addEventListener("click", downloadCsv);
    $("ma-chips").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-n]");
      if (b) toggleMa(Number(b.dataset.n));
    });
    $("ma-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const field = $("ma-input");
      const n = Number(field.value);
      if (!validDays(n)) { setMaMessage(`Enter a whole number of days from ${MA_MIN_DAYS} to ${MA_MAX_DAYS}.`); return; }
      field.value = "";
      toggleMa(n, true);
    });
  }

  async function main() {
    readHash();
    const status = $("status");
    status.hidden = false;
    status.textContent = "Loading market data…";
    try {
      const res = await fetch("data/market.json", { cache: "no-cache" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      data = await res.json();
    } catch (e) {
      status.textContent = "Could not load data/market.json (" + e.message + ").";
      $("updated").textContent = "";
      return;
    }
    dates = data.dates;
    const counts = { equity: 0, mutual_fund: 0, debenture: 0 };
    const opts = ['<option value="NEPSE">NEPSE Index</option>'];
    for (const [sym, kind] of data.securities) {
      typeOf[sym] = kind;
      counts[kind]++;
      opts.push(`<option value="${esc(sym)}">${TYPE_LABEL[kind]}</option>`);
    }
    $("symbols").innerHTML = opts.join("");
    for (const k of Object.keys(counts)) $("n-" + k).textContent = counts[k];
    $("updated").textContent = "Data through " + data.last_date + ".";

    createCharts();
    price = priceFromIndex();
    setPriceData();
    renderBreadthAll(false);
    applyRange();
    wireControls();
    status.hidden = true;
    if (state.symbol !== "NEPSE") await selectSymbol(state.symbol);
    // The price-axis width is measured on first paint and narrows the plot,
    // which would shift the range set above, so set it again once drawn.
    requestAnimationFrame(() => requestAnimationFrame(applyRange));
  }

  main();
})();
