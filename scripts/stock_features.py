"""Per-stock technical indicators, setups and events for the stock screener.

Everything here works on ONE security's rows (sorted by date) and on its own
trading days, so a share that skipped a day doesn't get a fake zero move.

Prices are back-adjusted for bonus/rights issues exactly like site/app.js
(loadCompany): wherever the raw close-to-close move differs from the effective
move (price_changes.effective_change), every earlier price is scaled by the
ratio. Without this a 30% bonus share looks like a 30% crash and every trend
reading breaks.

Data caveat (checked Nov 2026): before 2018-02-19 a stock's `open` is almost
always the previous close (98% of rows) and high/low are inconsistent with
open/close on ~40% of rows. Close-based readings use the full history for
warm-up; anything that needs a real open/high/low (ATR, ADX, Supertrend,
next-open returns) is only studied from RELIABLE_FROM on.

The methods are the widely published ones, kept to their textbook settings so
nothing is tuned to this history:

  Trend      SMA 20/50/150/200, Minervini trend template (8 rules), Weinstein
             stages on the 150-day (30-week) MA, Supertrend (10, 3),
             ADX/DMI (14)
  Momentum   IBD-style relative strength (40% last quarter + 20% each of the
             three before, ranked 1-99 across the market by the caller),
             12-1 month momentum, RSI (14 and Connors' 2), MACD (12, 26, 9),
             distance from the 52-week high (George & Hwang)
  Volume     relative turnover, 50-day up/down turnover (accumulation /
             distribution), Chaikin money flow (20), OBV-style 20-day balance,
             pocket pivots (Morales & Kacher)
  Volatility Bollinger (20, 2) inside Keltner (20, 1.5 ATR) = TTM squeeze,
             volatility contraction near highs (Minervini's VCP, simplified)
  Levels     Donchian 55-day breakouts (Turtle rules), auto support and
             resistance from clustered swing points
"""
import numpy as np
import pandas as pd

from price_changes import effective_change

RELIABLE_FROM = "2018-02-19"   # first date with a real open / high / low
MIN_HISTORY = 120              # trading days a share needs before it is scored

# code -> (label, direction, one-line description). Direction +1 bullish, -1 bearish.
EVENTS = {
    "brk55": ("55-day breakout", 1, "Closed above its highest high of the last 55 days on at least 1.5x normal turnover (Turtle/Donchian breakout with volume)"),
    "hi52": ("New 52-week high", 1, "Closed at its highest level in a year (52-week-high momentum)"),
    "pp": ("Pocket pivot", 1, "Up day on more turnover than any down day of the last 10, above its 10- and 50-day averages (Morales & Kacher)"),
    "sqz": ("Squeeze fired", 1, "Bollinger bands had been inside the Keltner channel for 5+ days and broke out upward (TTM squeeze)"),
    "stup": ("Supertrend turned up", 1, "Closed above the Supertrend (10, 3) line: the trailing-stop trend flipped to up"),
    "gc": ("Golden cross", 1, "50-day average crossed above the 200-day average"),
    "tt": ("Joined trend template", 1, "Passes all 8 of Minervini's trend-template rules, for the first time in 10+ days"),
    "s2": ("Entered stage 2", 1, "Price above a rising 30-week (150-day) average after a base (Weinstein stage 2)"),
    "pb": ("Pullback in uptrend", 1, "2-day RSI below 10 while the 50-day average is above the 200-day (Connors pullback)"),
    "vcp": ("Tight near highs", 1, "Volatility contracted to half its usual size within 12% of the 52-week high, trend intact (Minervini VCP, simplified)"),
    "macd": ("MACD bullish cross", 1, "MACD (12, 26) crossed above its 9-day signal line"),
    "vol": ("Volume surge up", 1, "Rose on at least 3x its 20-day average turnover"),
    "brkdn": ("55-day breakdown", -1, "Closed below its lowest low of the last 55 days on at least 1.5x normal turnover"),
    "lo52": ("New 52-week low", -1, "Closed at its lowest level in a year"),
    "stdn": ("Supertrend turned down", -1, "Closed below the Supertrend (10, 3) line: the trailing-stop trend flipped to down"),
    "dc": ("Death cross", -1, "50-day average crossed below the 200-day average"),
    "s4": ("Entered stage 4", -1, "Price below a falling 30-week (150-day) average (Weinstein stage 4)"),
    "dist": ("Heavy selling", -1, "Fell more than 3% on at least 3x its 20-day average turnover"),
    "ob": ("Overbought (RSI > 80)", -1, "14-day RSI crossed above 80"),
}
# Impulse events can repeat on consecutive days; keep only the first in this many days.
COOLDOWN = 10


# ---------------------------------------------------------------- price path
def adjusted_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Clean, bonus/rights-adjusted daily rows: date, o, h, l, c, amt, eff.

    Same rules as site/app.js loadCompany(): rows without a positive close are
    dropped (suspended / placeholder rows), a missing open/high/low falls back
    to the close, high/low are widened to include open and close, and earlier
    prices are scaled wherever the raw close-to-close move and the effective
    move disagree by more than 0.2%.
    """
    d = df[["published_date", "open", "high", "low", "close", "per_change", "traded_amount"]].copy()
    for k in ("open", "high", "low", "close", "per_change", "traded_amount"):
        d[k] = pd.to_numeric(d[k], errors="coerce")
    d = d[d["close"].notna() & (d["close"] > 0)]
    # 122 of the CSVs repeat some dates (a few re-appended blocks, out of order):
    # sort by date and keep the last row of each day, as app.js does.
    d["published_date"] = d["published_date"].astype(str)
    d = d.sort_values("published_date", kind="stable").drop_duplicates("published_date", keep="last").reset_index(drop=True)
    eff = effective_change(d).to_numpy(dtype=float)
    c = d["close"].to_numpy(dtype=float)
    raw = np.ones(len(c))
    raw[1:] = c[1:] / c[:-1]
    real = 1 + np.nan_to_num(eff) / 100
    step = np.where((np.abs(raw - real) > 0.002) & (real > 0), raw / real, 1.0)
    step[0] = 1.0
    # factor[k] = product of step[j] for j > k
    rev = np.cumprod(step[::-1])[::-1]
    factor = np.ones(len(c))
    factor[:-1] = rev[1:]

    def px(col):
        v = d[col].to_numpy(dtype=float)
        return np.where(np.isfinite(v) & (v > 0), v, c)

    o, h, lo = px("open"), px("high"), px("low")
    h, lo = np.maximum.reduce([h, o, c]), np.minimum.reduce([lo, o, c])
    return pd.DataFrame({
        "date": d["published_date"].astype(str).to_numpy(),
        "o": o * factor, "h": h * factor, "l": lo * factor, "c": c * factor,
        "amt": np.nan_to_num(d["traded_amount"].to_numpy(dtype=float)),
        "eff": eff,
    })


# ---------------------------------------------------------------- primitives
def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (the RSI/ATR/ADX average)."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(c: pd.Series, n: int) -> pd.Series:
    delta = c.diff()
    gain, loss = rma(delta.clip(lower=0), n), rma((-delta).clip(lower=0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + gain / loss)
    return out.where(loss > 0, 100.0).where(gain.notna())


def supertrend(h, l, c, atr, mult=3.0):
    """Supertrend line and direction (+1 up / -1 down), TradingView's definition."""
    n = len(c)
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub, flb = ub.copy(), lb.copy()
    line = np.full(n, np.nan)
    dirn = np.zeros(n, dtype=np.int8)
    start = np.argmax(np.isfinite(atr)) if np.isfinite(atr).any() else n
    if start < n:
        dirn[start] = 1
        line[start] = flb[start]
    for i in range(start + 1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if dirn[i - 1] == 1:
            dirn[i] = -1 if c[i] < flb[i] else 1
        else:
            dirn[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if dirn[i] == 1 else fub[i]
    return line, dirn


def weinstein_stage(c, ma, slope, flat=0.01):
    """1 basing, 2 advancing, 3 topping, 4 declining (on the 150-day MA)."""
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    last_trend = 0
    for i in range(n):
        if not (np.isfinite(ma[i]) and np.isfinite(slope[i])):
            continue
        if slope[i] > flat and c[i] > ma[i]:
            st = 2
        elif slope[i] < -flat and c[i] < ma[i]:
            st = 4
        else:
            st = 3 if last_trend == 2 else 1
        if st in (2, 4):
            last_trend = st
        out[i] = st
    return out


def first_in(flag: np.ndarray, gap: int = COOLDOWN) -> np.ndarray:
    """Keep a True only if no True was kept in the previous `gap` rows."""
    out = np.zeros(len(flag), dtype=bool)
    last = -10 ** 9
    for i in np.flatnonzero(flag):
        if i - last >= gap:
            out[i] = True
            last = i
    return out


def turned_on(flag: pd.Series) -> np.ndarray:
    f = flag.fillna(False).astype(bool)
    return (f & ~f.shift(1, fill_value=False)).to_numpy()


# ---------------------------------------------------------------- indicators
TT_RULES = [
    "Price above the 150- and 200-day averages",
    "150-day average above the 200-day",
    "200-day average rising for a month",
    "50-day average above the 150- and 200-day",
    "Price above the 50-day average",
    "At least 30% above the 52-week low",
    "Within 25% of the 52-week high",
    "Relative strength rating 70 or more",
]


def tt_rules(f: pd.DataFrame) -> dict:
    """Minervini's trend template, rules 1-7 (rule 8, RS >= 70, needs the cross-section)."""
    c = f.c
    return {
        0: (c > f.sma150) & (c > f.sma200),
        1: f.sma150 > f.sma200,
        2: f.sma200_up.astype(bool),
        3: (f.sma50 > f.sma150) & (f.sma50 > f.sma200),
        4: c > f.sma50,
        5: c >= 1.3 * f.lo250,
        6: c >= 0.75 * f.hi250,
    }


def indicators(a: pd.DataFrame) -> pd.DataFrame:
    """All readings for one security. `a` comes from adjusted_ohlcv()."""
    o, h, l, c, amt, eff = (a[k] for k in ("o", "h", "l", "c", "amt", "eff"))
    f = pd.DataFrame({"date": a["date"], "o": o, "h": h, "l": l, "c": c, "amt": amt, "eff": eff})
    f["age"] = np.arange(len(a))

    for n in (10, 20, 50, 150, 200):
        f[f"sma{n}"] = c.rolling(n).mean()
    f["sma200_up"] = f.sma200 > f.sma200.shift(21)
    f["slope50"] = f.sma50 / f.sma50.shift(10) - 1
    f["slope150"] = f.sma150 / f.sma150.shift(10) - 1
    for k in (5, 20, 60, 120, 250):
        f[f"r{k}"] = c / c.shift(k) - 1
    f["rs_raw"] = 0.4 * (c / c.shift(63)) + 0.2 * (c / c.shift(126)) + 0.2 * (c / c.shift(189)) + 0.2 * (c / c.shift(252))
    f["mom12_1"] = c.shift(21) / c.shift(252) - 1
    f["hi250"], f["lo250"] = c.rolling(250, min_periods=120).max(), c.rolling(250, min_periods=120).min()
    f["dist_hi"], f["dist_lo"] = c / f.hi250 - 1, c / f.lo250 - 1
    for n in (20, 50, 200):
        f[f"dist{n}"] = c / f[f"sma{n}"] - 1

    f["rsi14"], f["rsi2"] = rsi(c, 14), rsi(c, 2)
    macd = ema(c, 12) - ema(c, 26)
    f["macd"], f["macd_sig"] = macd, ema(macd, 9)
    f["macd_hist"] = f.macd - f.macd_sig
    f["macd_n"] = f.macd_hist / c

    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f["atr14"] = rma(tr, 14)
    f["atr_pct"] = f.atr14 / c
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=c.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=c.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, ndi = 100 * rma(pdm, 14) / f.atr14, 100 * rma(ndm, 14) / f.atr14
        dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    f["pdi"], f["ndi"], f["adx"] = pdi, ndi, rma(dx.fillna(0), 14)
    st, sd = supertrend(h.to_numpy(), l.to_numpy(), c.to_numpy(), rma(tr, 10).to_numpy())
    f["st"], f["st_dir"] = st, sd

    sd20 = c.rolling(20).std(ddof=0)
    f["bb_up"], f["bb_dn"] = f.sma20 + 2 * sd20, f.sma20 - 2 * sd20
    f["bb_width"] = 4 * sd20 / f.sma20
    f["bb_width_pct"] = f.bb_width.rolling(120, min_periods=60).rank(pct=True)
    kc_mid, kc_atr = ema(c, 20), rma(tr, 20)
    f["sqz_on"] = (f.bb_up < kc_mid + 1.5 * kc_atr) & (f.bb_dn > kc_mid - 1.5 * kc_atr)

    avg20 = amt.rolling(20).mean().shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        f["rel_vol"] = amt / avg20
        f["vol_trend"] = amt.rolling(5).mean() / amt.rolling(50).mean()
    f["liq20"] = amt.rolling(20).median()
    upd, dnd = eff > 0, eff < 0
    up50, dn50 = amt.where(upd, 0).rolling(50).sum(), amt.where(dnd, 0).rolling(50).sum()
    f["updown50"] = np.log((up50 + 1) / (dn50 + 1))
    rng = (h - l).replace(0, np.nan)
    mfm = (((c - l) - (h - c)) / rng).fillna(0)
    f["cmf20"] = (mfm * amt).rolling(20).sum() / amt.rolling(20).sum().replace(0, np.nan)
    signed_amt = amt * np.sign(eff.fillna(0))
    f["obv20"] = signed_amt.rolling(20).sum() / amt.rolling(20).sum().replace(0, np.nan)
    vol10 = eff.rolling(10).std()
    f["contraction"] = vol10 / eff.shift(10).rolling(60, min_periods=40).std()

    # Minervini trend template: 7 price rules here, the 8th (RS rating >= 70) needs the
    # cross-section and is added by the caller (see add_market_ranks).
    rules = tt_rules(f)
    f["tt_price"] = sum(r.astype(int) for r in rules.values()).where(f.sma200.notna() & f.hi250.notna())   # 0..7
    f["stage"] = weinstein_stage(c.to_numpy(), f.sma150.to_numpy(), f.slope150.to_numpy())

    # forward returns, tradeable: buy at the next session's open, hold h sessions
    nxt_o = o.shift(-1)
    for k in (5, 20):
        f[f"fwd{k}"] = c.shift(-k) / nxt_o - 1
    return f


def detect_events(f: pd.DataFrame) -> pd.DataFrame:
    """Boolean column per event code (see EVENTS) on top of indicators()."""
    c, h, l, eff = f.c, f.h, f.l, f.eff
    rv = f.rel_vol.fillna(0)
    ev = {}
    hh55, ll55 = h.shift(1).rolling(55).max(), l.shift(1).rolling(55).min()
    ev["brk55"] = first_in(((c > hh55) & (rv >= 1.5)).to_numpy())
    ev["brkdn"] = first_in(((c < ll55) & (rv >= 1.5)).to_numpy())
    prev_hi, prev_lo = c.shift(1).rolling(250, min_periods=120).max(), c.shift(1).rolling(250, min_periods=120).min()
    ev["hi52"] = first_in((c > prev_hi).to_numpy())
    ev["lo52"] = first_in((c < prev_lo).to_numpy())
    down_amt = f.amt.where(eff < 0, 0).shift(1).rolling(10).max()
    ev["pp"] = first_in(((eff > 0) & (f.amt > down_amt) & (c > f.sma10) & (c > f.sma50)).to_numpy())
    sq = f["sqz_on"].fillna(False).astype(bool)
    sq_run = sq.astype(int).groupby((~sq).cumsum()).cumsum()       # consecutive squeeze days
    fired = (~sq) & (sq_run.shift(1) >= 5) & (c > f.sma20) & (eff > 0)
    ev["sqz"] = fired.to_numpy()
    sd = pd.Series(f.st_dir.to_numpy(), index=f.index)
    ev["stup"] = ((sd == 1) & (sd.shift(1) == -1)).to_numpy()
    ev["stdn"] = ((sd == -1) & (sd.shift(1) == 1)).to_numpy()
    above = (f.sma50 > f.sma200).where(f.sma200.notna())
    ev["gc"] = ((above == 1) & (above.shift(1) == 0)).to_numpy()
    ev["dc"] = ((above == 0) & (above.shift(1) == 1)).to_numpy()
    stg = pd.Series(f.stage.to_numpy(), index=f.index)
    ev["s2"] = ((stg == 2) & (stg.shift(1) == 1)).to_numpy()
    ev["s4"] = ((stg == 4) & (stg.shift(1).isin([1, 3]))).to_numpy()
    ev["pb"] = first_in(((f.rsi2 < 10) & (f.sma50 > f.sma200) & (c > f.sma200)).to_numpy())
    tight = (f.contraction < 0.5) & (f.dist_hi > -0.12) & (f.tt_price >= 6)
    ev["vcp"] = first_in(tight.fillna(False).to_numpy())
    m_above = (f.macd > f.macd_sig).where(f.macd_sig.notna())
    ev["macd"] = ((m_above == 1) & (m_above.shift(1) == 0)).to_numpy()
    ev["vol"] = first_in(((rv >= 3) & (eff > 0)).to_numpy(), 5)
    ev["dist"] = first_in(((rv >= 3) & (eff < -3)).to_numpy(), 5)
    ev["ob"] = turned_on(f.rsi14 > 80)
    out = pd.DataFrame(ev, index=f.index)
    # Trend template needs the market-wide RS rating; filled in by add_market_ranks().
    out["tt"] = False
    return out


# ---------------------------------------------------------------- levels
def support_resistance(f: pd.DataFrame, lookback=250, wing=5, max_levels=3):
    """Auto support/resistance: swing highs/lows (a bar higher/lower than the `wing`
    bars on each side) over the last `lookback` sessions, clustered within half an
    ATR, ranked by touches then recency. Returns (supports, resistances) as lists of
    {"p": price, "n": touches}, nearest first."""
    t = f.tail(lookback).reset_index(drop=True)
    if len(t) < 2 * wing + 5:
        return [], []
    h, l, c = t.h.to_numpy(), t.l.to_numpy(), t.c.to_numpy()
    atr = float(t.atr14.iloc[-1]) if np.isfinite(t.atr14.iloc[-1]) else float(np.nanstd(c)) * 0.1
    pts = []
    for i in range(wing, len(t) - wing):
        if h[i] == h[i - wing:i + wing + 1].max():
            pts.append((h[i], i))
        if l[i] == l[i - wing:i + wing + 1].min():
            pts.append((l[i], i))
    pts.sort()
    clusters = []
    for p, i in pts:
        if clusters and p - clusters[-1]["hi"] <= 0.5 * atr:
            cl = clusters[-1]
            cl["ps"].append(p)
            cl["hi"] = p
            cl["last"] = max(cl["last"], i)
        else:
            clusters.append({"ps": [p], "hi": p, "last": i})
    last = c[-1]
    levels = [{"p": float(np.mean(cl["ps"])), "n": len(cl["ps"]), "last": cl["last"]} for cl in clusters]
    rank = lambda x: (-x["n"], -x["last"])  # noqa: E731
    sup = sorted([x for x in levels if x["p"] < last * 0.995], key=rank)[:max_levels]
    res = sorted([x for x in levels if x["p"] > last * 1.005], key=rank)[:max_levels]
    clean = lambda xs, rev: [{"p": round(x["p"], 2), "n": x["n"]} for x in sorted(xs, key=lambda x: x["p"], reverse=rev)]  # noqa: E731
    return clean(sup, True), clean(res, False)
