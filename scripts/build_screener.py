"""Daily stock screener, AI ranking and setup scanner for the site.

    python scripts/build_screener.py          # after build_site.py: writes into _site/data
    python scripts/build_screener.py --log    # CI: also upsert today's suggestions into data/picks-log.csv

Writes
  _site/data/screener.json     every share's latest readings, today's AI
                               suggestions, setups that fired today, sector
                               rotation (RRG) and the evidence behind it all
  _site/data/stocks/SYM.json   the last DETAIL_DAYS sessions of one share with
                               the indicators the stock chart draws

How it decides (all of it re-measured on every build, nothing hand-typed)
  * stock_features.py computes ~30 textbook readings and 19 textbook setups
    per share on bonus-adjusted prices.
  * Each setup's track record: the share's return from the NEXT session's
    open over 20 sessions, minus the average share's over the same window
    ("excess"), for 2018-06..2021 and 2022..now separately. A setup only
    counts as working if both halves agree.
  * The AI is a gradient-boosted tree model (scikit-learn) that ranks shares
    by their expected 20-session excess return from all readings at once
    (each turned into a 0-1 percentile within that day, so 2018 and 2026
    levels are comparable). It is tested walk-forward: for each year since
    2020 it is trained only on data that ended before that year started
    (with a 22-session gap so no training target overlaps the test year),
    then scored on that year. The reported accuracy is that out-of-sample
    record. Today's scores come from a model trained on everything whose
    20-session outcome is already known.
  * Universe each day: ordinary shares with 120+ sessions of history, leaving
    out the least-traded 20%. Suggestions also need a median daily turnover
    of at least PICK_LIQ_MIN and a trade on the latest day.
"""
import argparse
import csv
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from securities import EQUITY, load_companies  # noqa: E402
from sectors import UNCLASSIFIED, load_sectors  # noqa: E402
from stock_features import (  # noqa: E402
    EVENTS, MIN_HISTORY, TT_RULES, adjusted_ohlcv, detect_events, indicators, support_resistance, tt_rules,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "_site"
INDEX_PATH = ROOT / "data" / "nepse-index.csv"
SIGNAL_LOG = ROOT / "data" / "signal-log.csv"
PICKS_LOG = ROOT / "data" / "picks-log.csv"

PANEL_FROM = "2017-01-01"     # cross-sectional work starts here (indicators warm up on full history)
STUDY_FROM = "2018-06-01"     # real open/high/low from 2018-02-19, plus warm-up
SPLIT = "2022-01-01"          # the setup study's two halves
FIRST_TEST_YEAR = 2020
HORIZON = 20
EMBARGO = HORIZON + 2
LIQ_RANK_MIN = 0.2
PICK_LIQ_MIN = 25e5           # NPR 25 lakh median daily turnover
COST_ROUND_TRIP = 0.01        # 1% per full turnover in the portfolio check (commission, fees, slippage)
DETAIL_DAYS = 500
SPARK_DAYS = 90
N_PICKS, N_AVOID, MAX_PER_SECTOR = 8, 6, 2
RECENT_SESSIONS = 5           # "recent setups" window

# Model inputs: each becomes q_<name>, its percentile within the day's universe.
RANKED = [
    "r5", "r20", "r60", "r120", "r250", "mom12_1", "rs_raw", "dist_hi", "dist_lo", "dist20", "dist50", "dist200",
    "rsi14", "rsi2", "macd_n", "adx", "atr_pct", "bb_width_pct", "rel_vol", "vol_trend", "updown50", "cmf20",
    "obv20", "contraction", "liq20", "slope50", "slope150", "rel_sec20", "sec_r20", "sec_r60",
]
UNRANKED = ["tt_price", "stage", "st_dir", "mkt_r20", "mkt_breadth50"]
FEATURES = ["q_" + x for x in RANKED] + UNRANKED

# reading -> (name, said when high, said when low)
LABELS = {
    "r5": ("1-week return", "Up sharply this week", "Down sharply this week"),
    "r20": ("1-month return", "Strong month", "Weak month"),
    "r60": ("3-month return", "Strong 3 months", "Weak 3 months"),
    "r120": ("6-month return", "Strong 6 months", "Weak 6 months"),
    "r250": ("1-year return", "Strong 1-year return", "Weak 1-year return"),
    "mom12_1": ("12-1 month momentum", "Strong 12-month momentum", "Weak 12-month momentum"),
    "rs_raw": ("IBD relative strength", "High relative strength", "Low relative strength"),
    "dist_hi": ("Distance from 52-week high", "Near its 52-week high", "Far below its 52-week high"),
    "dist_lo": ("Distance from 52-week low", "Far above its 52-week low", "Near its 52-week low"),
    "dist20": ("Price vs 20-day average", "Stretched above 20-day avg", "Below its 20-day avg"),
    "dist50": ("Price vs 50-day average", "Above its 50-day avg", "Below its 50-day avg"),
    "dist200": ("Price vs 200-day average", "Above its 200-day avg", "Below its 200-day avg"),
    "rsi14": ("RSI 14", "RSI high", "RSI low"),
    "rsi2": ("RSI 2 (short-term)", "Short-term overbought", "Short-term oversold"),
    "macd_n": ("MACD histogram", "MACD rising", "MACD falling"),
    "adx": ("ADX trend strength", "Strong trend (ADX)", "No clear trend (ADX)"),
    "atr_pct": ("Volatility (ATR %)", "Volatile", "Calm, steady price"),
    "bb_width_pct": ("Bollinger width vs own history", "Bands wide", "Bands tight"),
    "rel_vol": ("Today's turnover vs normal", "Heavy turnover today", "Quiet today"),
    "vol_trend": ("Turnover trend (5 vs 50 days)", "Turnover picking up", "Turnover drying up"),
    "updown50": ("Up- vs down-day turnover (50d)", "Accumulation", "Distribution"),
    "cmf20": ("Chaikin money flow", "Closing near highs (CMF)", "Closing near lows (CMF)"),
    "obv20": ("20-day turnover balance (OBV)", "Buying pressure", "Selling pressure"),
    "contraction": ("Volatility contraction", "Volatility expanding", "Volatility contracting"),
    "liq20": ("Liquidity", "Heavily traded", "Thinly traded"),
    "slope50": ("50-day average slope", "50-day trend rising", "50-day trend falling"),
    "slope150": ("150-day average slope", "Long trend rising", "Long trend falling"),
    "rel_sec20": ("Month vs its sector", "Beating its sector", "Lagging its sector"),
    "sec_r20": ("Sector, 1 month", "Sector strong this month", "Sector weak this month"),
    "sec_r60": ("Sector, 3 months", "Sector strong (3 months)", "Sector weak (3 months)"),
    "tt_price": ("Trend-template rules met", "Trend template intact", "Trend template broken"),
    "stage": ("Weinstein stage", "", ""),
    "st_dir": ("Supertrend direction", "Supertrend up", "Supertrend down"),
    "mkt_r20": ("Market, 1 month", "", ""),
    "mkt_breadth50": ("Market breadth (% above 50d)", "", ""),
}

# Globally known methods, measured as rankers on NEPSE (sign as published: high = bullish).
METHODS = [
    ("ai", "AI model (gradient-boosted trees)"),
    ("rs_raw", "IBD relative strength"),
    ("mom12_1", "12-1 month momentum (Jegadeesh-Titman)"),
    ("dist_hi", "Near 52-week high (George-Hwang)"),
    ("tt_count", "Minervini trend template"),
    ("trend", "Trend rating (MA + oscillator votes)"),
    ("stage2", "Weinstein stage 2"),
    ("st_dir", "Supertrend direction"),
    ("macd_n", "MACD histogram"),
    ("rsi14", "RSI 14"),
    ("cmf20", "Chaikin money flow"),
    ("updown50", "Up/down turnover (accumulation)"),
    ("rel_vol", "Relative volume"),
    ("neg_rsi2", "Short-term reversal (low RSI 2)"),
    ("neg_atr", "Low volatility"),
    ("neg_liq", "Smaller / less traded"),
]


def r(v, k=2):
    if v is None or (isinstance(v, (float, np.floating)) and not np.isfinite(v)):
        return None
    return round(float(v), k)


def rlist(a, k=2):
    return [r(x, k) for x in a]


# ---------------------------------------------------------------- panel
def build_panel():
    sector_of = load_sectors()
    frames, per_stock = [], {}
    for sym, kind, df in load_companies():
        if kind != EQUITY:
            continue
        a = adjusted_ohlcv(df)
        if len(a) < 60:
            continue
        f = indicators(a)
        e = detect_events(f)
        f = pd.concat([f, e.add_prefix("ev_")], axis=1)
        f["sym"] = sym
        f["sector"] = sector_of.get(sym, UNCLASSIFIED)
        per_stock[sym] = f
        frames.append(f[f.date >= PANEL_FROM])
    p = pd.concat(frames, ignore_index=True)
    p["date"] = pd.to_datetime(p["date"])
    by = p.groupby("date")
    p["liq_rank"] = by.liq20.rank(pct=True)
    p["rs_rating"] = (by.rs_raw.rank(pct=True) * 98 + 1).round()
    p["uni"] = (p.age >= MIN_HISTORY) & (p.liq_rank >= LIQ_RANK_MIN) & p.liq20.notna()

    # trend template rule 8 (RS >= 70) and the "joined the template" setup
    p = p.sort_values(["sym", "date"]).reset_index(drop=True)
    p["tt_count"] = p.tt_price + (p.rs_rating >= 70).astype(float)
    p["tt_full"] = ((p.tt_price == 7) & (p.rs_rating >= 70)).astype(int)
    prev10 = p.groupby("sym").tt_full.transform(lambda s: s.shift(1).rolling(10, min_periods=1).max()).fillna(0)
    p["ev_tt"] = (p.tt_full == 1) & (prev10 == 0)
    p["stage2"] = (p.stage == 2).astype(float)
    p["trend"] = trend_score(p)
    p["neg_rsi2"], p["neg_atr"], p["neg_liq"] = -p.rsi2, -p.atr_pct, -p.liq20

    # cross-sectional readings, within each day's universe only (NaN outside it)
    pu = p[p.uni]
    g, gs = pu.groupby("date"), pu.groupby(["date", "sector"])
    cols = {
        "mkt_r20": g.r20.transform("median"),
        "mkt_breadth50": g.dist50.transform(lambda x: (x > 0).mean()),
        "sec_r20": gs.r20.transform("median"),
        "sec_r60": gs.r60.transform("median"),
        "ex20": pu.fwd20 - g.fwd20.transform("mean"),
        "y": g.fwd20.rank(pct=True),
    }
    cols["rel_sec20"] = pu.r20 - cols["sec_r20"]
    pu = pu.assign(rel_sec20=cols["rel_sec20"], sec_r20=cols["sec_r20"], sec_r60=cols["sec_r60"])
    g = pu.groupby("date")
    for x in RANKED:
        cols["q_" + x] = g[x].rank(pct=True)
    p = pd.concat([p, pd.DataFrame(cols).reindex(p.index)], axis=1)
    # per-stock tables need the template events too
    tt_map = p.loc[p.ev_tt, ["sym", "date"]]
    for sym, ds in tt_map.groupby("sym").date:
        f = per_stock[sym]
        f["ev_tt"] = f.date.isin(set(ds.dt.strftime("%Y-%m-%d")))
    return p, per_stock


def trend_score(p):
    """Descriptive trend rating, -100..100: half moving-average votes, half oscillator
    votes (in the spirit of TradingView's technical rating). It describes the chart;
    whether it predicts anything on NEPSE is measured in METHODS."""
    c = p.c
    sgn = lambda x: np.sign(x).where(x.notna())  # noqa: E731
    ma = pd.concat([sgn(c - p.sma10), sgn(c - p.sma20), sgn(c - p.sma50), sgn(c - p.sma150), sgn(c - p.sma200),
                    p.st_dir.astype(float).replace(0, np.nan), sgn(p.sma50 - p.sma200)], axis=1).mean(axis=1)
    osc = pd.concat([
        pd.Series(np.select([p.rsi14 > 55, p.rsi14 < 45], [1.0, -1.0], 0.0), index=p.index).where(p.rsi14.notna()),
        sgn(p.macd_hist),
        pd.Series(np.where(p.adx > 20, np.sign(p.pdi - p.ndi), 0.0), index=p.index).where(p.adx.notna()),
        pd.Series(np.select([p.cmf20 > 0.05, p.cmf20 < -0.05], [1.0, -1.0], 0.0), index=p.index).where(p.cmf20.notna()),
        pd.Series(np.select([p.dist_hi > -0.10, p.dist_hi < -0.30], [1.0, -1.0], 0.0), index=p.index).where(p.dist_hi.notna()),
    ], axis=1).mean(axis=1)
    return ((ma + osc) / 2 * 100).round()


TREND_WORDS = [(50, "Strong uptrend"), (15, "Uptrend"), (-15, "Sideways"), (-50, "Downtrend"), (-101, "Strong downtrend")]


def trend_word(v):
    if v is None or not np.isfinite(v):
        return "–"
    return next(w for t, w in TREND_WORDS if v >= t)


# ---------------------------------------------------------------- evidence helpers
def daily_ic(df, col, target="ex20"):
    """Mean over days of the Spearman correlation between col and target."""
    d = df[["date", col, target]].dropna()
    if d.empty:
        return pd.Series(dtype=float)
    d = d.assign(a=d.groupby("date")[col].rank(), b=d.groupby("date")[target].rank())
    g = d.groupby("date")
    n = g.size()
    ma, mb = g.a.transform("mean"), g.b.transform("mean")
    d["ab"], d["aa"], d["bb"] = (d.a - ma) * (d.b - mb), (d.a - ma) ** 2, (d.b - mb) ** 2
    s = d.groupby("date")[["ab", "aa", "bb"]].sum()
    ic = s.ab / np.sqrt(s.aa * s.bb)
    return ic[(n >= 20) & np.isfinite(ic)]


def event_study(p):
    s = p[p.uni & (p.date >= STUDY_FROM) & p.ex20.notna()]
    halves = {"h1": s.date < SPLIT, "h2": s.date >= SPLIT}
    base = {k: float((s[m].ex20 > 0).mean() * 100) for k, m in halves.items()}
    out = {}
    for code, (label, textbook, desc) in EVENTS.items():
        rec = {"label": label, "textbook": textbook, "desc": desc}
        avgs = []
        for k, m in halves.items():
            e = s[m & s["ev_" + code]].ex20
            rec[k] = {"n": int(len(e)), "ex": r(e.mean() * 100, 2) if len(e) else None,
                      "hit": r((e > 0).mean() * 100, 0) if len(e) else None}
            avgs.append(e.mean() * 100 if len(e) >= 40 else np.nan)
        a, b = avgs
        if not (np.isfinite(a) and np.isfinite(b)):
            verdict = "thin"
        elif a >= 0.25 and b >= 0.25:
            verdict = "up"
        elif a <= -0.25 and b <= -0.25:
            verdict = "down"
        else:
            verdict = "none"
        rec["verdict"] = verdict
        out[code] = rec
    return out, base


def model_matrix(p):
    return p[FEATURES].astype(float)


def fit_model(train):
    from sklearn.ensemble import HistGradientBoostingRegressor
    m = HistGradientBoostingRegressor(max_iter=250, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=400,
                                      l2_regularization=1.0, random_state=0)
    m.fit(model_matrix(train), train.y)
    return m


def train_rows(p, dates, until=None):
    t = p[p.uni & p.y.notna() & (p.date >= STUDY_FROM)]
    if until is not None:
        t = t[t.date <= until]
    return t[t.date.isin(set(dates[::3]))]   # every 3rd day: neighbours share 19 of 20 target days


def walk_forward(p):
    """Out-of-sample AI scores for every universe row since FIRST_TEST_YEAR, plus the
    final model (trained on everything with a known outcome) and its cutoff."""
    u = p[p.uni & (p.date >= STUDY_FROM)]
    dates = np.array(sorted(u.date.unique()))
    preds = pd.Series(np.nan, index=p.index)
    last_year = pd.Timestamp(dates[-1]).year
    for year in range(FIRST_TEST_YEAR, last_year + 1):
        start = np.searchsorted(dates, np.datetime64(f"{year}-01-01"))
        if start >= len(dates) or start - EMBARGO < 50:
            continue
        model = fit_model(train_rows(p, dates, dates[start - EMBARGO]))
        test = u[(u.date >= f"{year}-01-01") & (u.date < f"{year + 1}-01-01")]
        preds.loc[test.index] = model.predict(model_matrix(test))
    final_train = train_rows(p, dates)
    final = fit_model(final_train)
    cutoff = final_train.date.max()
    after = u[u.date > cutoff]
    preds.loc[after.index] = final.predict(model_matrix(after))
    return preds, final, cutoff, len(final_train)


def portfolio_check(p):
    """Every 20 sessions buy the AI's top 10 / bottom 10 of the universe (out-of-sample
    scores), hold 20 sessions from the next open; versus the average share."""
    s = p[p.uni & p.ai_raw.notna() & p.fwd20.notna()]
    dates = np.array(sorted(s.date.unique()))[::HORIZON]
    prev, rows = set(), []
    for d in dates:
        g = s[s.date == d]
        top, bot = g.nlargest(10, "ai_raw"), g.nsmallest(10, "ai_raw")
        cur = set(top.sym)
        turn = 1.0 if not prev else 1 - len(cur & prev) / 10
        prev = cur
        rows.append((d, top.fwd20.mean() - turn * COST_ROUND_TRIP, top.fwd20.mean(), bot.fwd20.mean(), g.fwd20.mean(), turn))
    b = pd.DataFrame(rows, columns=["d", "top", "top_gross", "bot", "mkt", "turn"])
    years = len(b) * HORIZON / 240
    cagr = lambda x: (float((1 + x).prod()) ** (1 / years) - 1) * 100  # noqa: E731
    curve = lambda x: rlist((1 + x).cumprod() * 100, 1)  # noqa: E731
    return {
        "dates": [pd.Timestamp(d).strftime("%Y-%m-%d") for d in b.d],
        "top": curve(b.top), "bot": curve(b.bot), "mkt": curve(b.mkt),
        "cagr": {"top": r(cagr(b.top), 1), "top_gross": r(cagr(b.top_gross), 1), "bot": r(cagr(b.bot), 1), "mkt": r(cagr(b.mkt), 1)},
        "turnover": r(b.turn.mean() * 100, 0), "cost": COST_ROUND_TRIP * 100,
        "beat": r((b.top > b.mkt).mean() * 100, 0), "periods": len(b),
    }


def model_evidence(p, final, cutoff, n_train):
    s = p[p.uni & p.ai_raw.notna() & p.ex20.notna() & (p.date.dt.year >= FIRST_TEST_YEAR)]
    ic = daily_ic(s, "ai_raw")
    by_year = ic.groupby(ic.index.year).mean()
    dec = s.assign(dec=np.minimum((s.groupby("date").ai_raw.rank(pct=True) * 10).astype(int), 9))
    dmean = dec.groupby(["date", "dec"]).ex20.mean().groupby("dec").mean() * 100
    yr = dec.assign(Y=dec.date.dt.year)
    tb = yr[yr.dec.isin([0, 9])].groupby(["Y", "dec", "date"]).ex20.mean().groupby(["Y", "dec"]).mean().unstack() * 100

    # drivers: permutation importance on the latest out-of-sample year
    from sklearn.inspection import permutation_importance
    recent = s[s.date >= s.date.max() - pd.Timedelta(days=365)]
    recent = recent.sample(min(len(recent), 30000), random_state=0)
    pi = permutation_importance(final, model_matrix(recent), recent.y, n_repeats=3, random_state=0, scoring="r2")
    imp = pd.Series(np.clip(pi.importances_mean, 0, None), index=FEATURES)
    imp = imp / imp.sum() if imp.sum() > 0 else imp
    drivers = [[LABELS[k[2:] if k.startswith("q_") else k][0], r(v * 100, 1), k] for k, v in imp.sort_values(ascending=False).head(10).items()]

    methods = []
    for col, name in METHODS:
        x = s if col == "ai" else p[p.uni & p.ex20.notna() & (p.date.dt.year >= FIRST_TEST_YEAR)]
        v = daily_ic(x, "ai_raw" if col == "ai" else col)
        yearly = v.groupby(v.index.year).mean()
        methods.append({"name": name, "ic": r(v.mean(), 3), "pos_years": int((yearly > 0).sum()), "years": int(len(yearly))})
    return {
        "ic": r(ic.mean(), 3),
        "ic_by_year": [[int(y), r(v, 3)] for y, v in by_year.items()],
        "deciles": rlist(dmean.reindex(range(10)).to_numpy(), 2),
        "top_bot_by_year": [[int(y), r(row.get(9), 2), r(row.get(0), 2)] for y, row in tb.iterrows()],
        "drivers": drivers,
        "methods": methods,
        "trained_through": cutoff.strftime("%Y-%m-%d"),
        "train_rows": int(n_train),
        "first_test_year": FIRST_TEST_YEAR,
        "n_features": len(FEATURES),
        "horizon": HORIZON,
        "portfolio": portfolio_check(p),
    }, imp


# ---------------------------------------------------------------- RRG
def rrg(p, n_ratio=50, n_mom=10, tail=6, step=5):
    """Relative Rotation Graph of sectors against the average share (equal weight).

    Open approximation of de Kempenaer's RRG, on daily data then sampled every
    `step` sessions for the tail:
      RS           sector level / market level (equal-weight, bonus-adjusted)
      RS-Ratio     100 x smoothed RS / its `n_ratio`-session average   (> 100: outperforming)
      RS-Momentum  100 x RS-Ratio / RS-Ratio `n_mom` sessions earlier  (> 100: improving)
    """
    s = p[(p.age >= 20) & p.eff.notna() & (p.date >= "2018-01-01")]
    daily = s.groupby(["date", "sector"]).eff.mean().unstack() / 100
    mkt = s.groupby("date").eff.mean() / 100
    lvl = (1 + daily.fillna(0)).cumprod()
    mlvl = (1 + mkt.fillna(0)).cumprod()
    rs = lvl.div(mlvl, axis=0).ewm(span=15, adjust=False).mean()
    ratio = 100 * rs / rs.rolling(n_ratio).mean()
    mom = (100 * ratio / ratio.shift(n_mom)).ewm(span=10, adjust=False).mean()
    pick = np.arange(len(ratio) - 1, -1, -step)[:tail][::-1]
    counts = p[p.date == p.date.max()].groupby("sector").size()
    out = {}
    for sec in ratio.columns:
        if sec == UNCLASSIFIED or counts.get(sec, 0) < 3:
            continue
        pts = [[r(ratio[sec].iloc[i], 2), r(mom[sec].iloc[i], 2)] for i in pick]
        if all(v is not None for pt in pts for v in pt):
            out[sec] = pts
    weeks = [ratio.index[i].strftime("%Y-%m-%d") for i in pick]
    return {"weeks": weeks, "sectors": out, "n_ratio": n_ratio, "n_mom": n_mom}


def quadrant(pt):
    x, y = pt
    return "Leading" if x >= 100 and y >= 100 else "Weakening" if x >= 100 else "Lagging" if y < 100 else "Improving"


# ---------------------------------------------------------------- suggestions
def reasons(row, imp, sign, good, k=3):
    """The stock's most extreme readings among the model's main drivers, worded."""
    cands = []
    for feat, w in imp.items():
        if not feat.startswith("q_") or w <= 0:
            continue
        v = row.get(feat)
        if v is None or not np.isfinite(v):
            continue
        name = feat[2:]
        sg = sign.get(feat, 0)
        lean = (v - 0.5) * sg            # > 0: this reading pushes the AI score up
        if (lean > 0) == good and abs(v - 0.5) >= 0.3:
            word = LABELS[name][1] if v > 0.5 else LABELS[name][2]
            share = max(1, int(round((1 - v if v > 0.5 else v) * 100)))
            cands.append((w * abs(lean), word, f"{'top' if v > 0.5 else 'bottom'} {share}%"))
    cands.sort(reverse=True)
    return [[w, t] for _, w, t in cands[:k]]


def driver_signs(today, imp):
    sign = {}
    for feat in imp.index:
        if feat.startswith("q_"):
            c = today[[feat, "ai_raw"]].dropna()
            sign[feat] = float(np.sign(c[feat].corr(c.ai_raw, method="spearman"))) if len(c) > 20 else 0.0
    return sign


def pick_list(cands, n, ascending, key="ai_raw"):
    """Best n rows by `key`, at most MAX_PER_SECTOR from any one sector."""
    out, per = [], {}
    for _, row in cands.sort_values(key, ascending=ascending).iterrows():
        if per.get(row.sector, 0) >= MAX_PER_SECTOR:
            continue
        per[row.sector] = per.get(row.sector, 0) + 1
        out.append(row)
        if len(out) == n:
            break
    return out


# ---------------------------------------------------------------- picks log (forward test)
LOG_FIELDS = ["date", "list", "rank", "symbol", "sector", "close", "ai", "logged_at_utc"]


def update_picks_log(last_date, picks, avoid):
    rows = []
    if PICKS_LOG.exists():
        with PICKS_LOG.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    if rows and max(x["date"] for x in rows) > last_date:
        print(f"picks log already has a newer day than {last_date}; not touched")
        return
    rows = [x for x in rows if x["date"] != last_date]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, lst in (("pick", picks), ("avoid", avoid)):
        for k, row in enumerate(lst, 1):
            rows.append({"date": last_date, "list": name, "rank": k, "symbol": row.sym, "sector": row.sector,
                         "close": r(row.c, 2), "ai": int(row.ai), "logged_at_utc": now})
    with PICKS_LOG.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"picks log: {len(picks)} picks + {len(avoid)} avoid for {last_date}")


def live_record(p):
    """How the logged suggestions actually did (from the next open, 20 sessions)."""
    if not PICKS_LOG.exists():
        return {"days": 0, "since": None}
    log = pd.read_csv(PICKS_LOG)
    if log.empty:
        return {"days": 0, "since": None}
    log["date"] = pd.to_datetime(log.date)
    look = p[["sym", "date", "fwd20", "ex20"]].rename(columns={"sym": "symbol"})
    m = log.merge(look, on=["symbol", "date"], how="left")
    done = m[m.ex20.notna()]
    out = {"days": int(log.date.nunique()), "since": log.date.min().strftime("%Y-%m-%d"), "complete_days": int(done.date.nunique())}
    for name in ("pick", "avoid"):
        d = done[done.list == name].groupby("date").ex20.mean()
        out[name] = {"ex": r(d.mean() * 100, 2) if len(d) else None, "beat": r((d > 0).mean() * 100, 0) if len(d) else None}
    return out


# ---------------------------------------------------------------- main
def main(write_log=False):
    t0 = time.time()
    p, per_stock = build_panel()
    print(f"panel {len(p):,} rows, {p.sym.nunique()} shares ({time.time() - t0:.0f}s)")
    preds, final, cutoff, n_train = walk_forward(p)
    p = p.assign(ai_raw=preds)
    p = p.assign(ai_pct=p[p.ai_raw.notna()].groupby("date").ai_raw.rank(pct=True).reindex(p.index))
    print(f"model: walk-forward done, final trained through {cutoff:%Y-%m-%d} on {n_train:,} rows ({time.time() - t0:.0f}s)")

    last = p.date.max()
    last_s = last.strftime("%Y-%m-%d")
    events, base_hit = event_study(p)
    evidence, imp = model_evidence(p, final, cutoff, n_train)
    print(f"evidence: out-of-sample IC {evidence['ic']}, by year {evidence['ic_by_year']} ({time.time() - t0:.0f}s)")

    sessions = np.array(sorted(p.date.unique()))
    recent_from = sessions[-RECENT_SESSIONS]
    active = set(p.loc[p.date >= recent_from, "sym"])      # traded in the last few sessions
    rows_of = dict(tuple(p.groupby("sym")))
    today = p[(p.date == last)].copy()
    today["ai"] = (today.ai_pct * 98 + 1).round()
    sign = driver_signs(today, imp)

    # ------------------------------------------------ per-share records
    index = pd.read_csv(INDEX_PATH).drop_duplicates("published_date", keep="last").set_index("published_date")
    rr = rrg(p)
    stocks, ev_today = [], {}
    latest_rows = {}
    for sym, f in per_stock.items():
        if sym not in active:
            continue
        row = rows_of[sym].iloc[-1]
        latest_rows[sym] = row
        tail = f.tail(SPARK_DAYS)
        recent = []
        ev_cols = [c for c in f.columns if c.startswith("ev_")]
        rec_rows = f[f.date >= pd.Timestamp(recent_from).strftime("%Y-%m-%d")]
        for code in EVENTS:
            hits = rec_rows.index[rec_rows["ev_" + code].astype(bool)] if "ev_" + code in ev_cols else []
            if len(hits):
                ago = int(len(f) - 1 - hits[-1])
                recent.append([code, ago])
                if ago == 0 and row.date == last:
                    ev_today.setdefault(code, []).append(sym)
        ai = row.get("ai_pct")
        stocks.append({
            "s": sym, "sec": row.sector, "d": row.date.strftime("%Y-%m-%d"),
            "c": r(row.c), "ch1": r(row.eff, 2), "ch5": r(row.r5 * 100, 1), "ch20": r(row.r20 * 100, 1),
            "ch60": r(row.r60 * 100, 1), "ch250": r(row.r250 * 100, 1),
            "liq": r(row.liq20 / 1e5, 1), "ai": None if ai is None or not np.isfinite(ai) else int(round(ai * 98 + 1)),
            "tr": r(row.trend, 0), "rs": r(row.rs_rating, 0), "tt": r(row.tt_count, 0), "stg": int(row.stage),
            "sd": int(row.st_dir), "rsi": r(row.rsi14, 0), "adx": r(row.adx, 0), "rv": r(row.rel_vol, 1),
            "dh": r(row.dist_hi * 100, 1), "ev": sorted(recent, key=lambda x: x[1]),
            "sp": rlist(tail.c.to_numpy(), 2), "uni": bool(row.uni),
        })

    # ------------------------------------------------ suggestions
    cands = today[today.uni & today.ai_raw.notna() & (today.liq20 >= PICK_LIQ_MIN)]
    picks, avoid = pick_list(cands, N_PICKS, False), pick_list(cands, N_AVOID, True)
    sugg = lambda rows, good: [{"s": x.sym, "why": reasons(x, imp, sign, good)} for x in rows]  # noqa: E731
    # Technical leaders: the textbook view (trend votes + IBD RS + trend template), shown
    # with the AI's opinion next to it, because on NEPSE the two often disagree.
    lead = today[today.liq20.notna() & (today.liq20 >= PICK_LIQ_MIN) & (today.age >= MIN_HISTORY)].copy()
    lead["lead"] = lead.trend.fillna(-100) / 100 + (lead.rs_rating.fillna(1) - 50) / 50 + (lead.tt_count.fillna(0) - 4) / 4
    leaders = pick_list(lead, N_PICKS, False, "lead")
    leaders_out = [{"s": x.sym, "why": reasons(x, imp, sign, not (np.isfinite(x.ai_pct) and x.ai_pct < 0.5))
                    if np.isfinite(x.ai_raw) else []} for x in leaders]
    setups = []
    for code, syms in ev_today.items():
        liquid = [s for s in syms if latest_rows[s].liq20 >= PICK_LIQ_MIN / 2]
        if liquid:
            liquid.sort(key=lambda s: -(latest_rows[s].get("ai_pct") if np.isfinite(latest_rows[s].get("ai_pct", np.nan)) else -1))
            setups.append({"code": code, "syms": liquid})
    order = {"up": 0, "none": 1, "thin": 2, "down": 3}
    setups.sort(key=lambda x: (order[events[x["code"]]["verdict"]], -len(x["syms"])))

    # ------------------------------------------------ sectors
    sec_rows = []
    tdf = pd.DataFrame(stocks)
    for sec, g in tdf.groupby("sec"):
        if sec == UNCLASSIFIED:
            continue
        pt = rr["sectors"].get(sec)
        sec_rows.append({"name": sec, "n": int(len(g)), "ch1": r(g.ch1.mean(), 2), "ch5": r(g.ch5.mean(), 1),
                         "ch20": r(g.ch20.mean(), 1), "ch60": r(g.ch60.mean(), 1),
                         "up": r((g.tr > 15).mean() * 100, 0), "ai": r(g.ai.dropna().mean(), 0) if g.ai.notna().any() else None,
                         "q": quadrant(pt[-1]) if pt else None, "q_prev": quadrant(pt[-2]) if pt and len(pt) > 1 else None})

    # ------------------------------------------------ market regime (from the breadth pipeline)
    regime = {}
    if SIGNAL_LOG.exists():
        sl = pd.read_csv(SIGNAL_LOG)
        if len(sl):
            x = sl.iloc[-1]
            regime = {"date": str(x.date), "score": int(x.score), "flow": r(x.money_flow_10d, 1),
                      "adv": int(x.advancers), "dec": int(x.decliners)}
    ix = index["close"].astype(float)
    regime.update({"nepse": r(ix.iloc[-1]), "nepse_chg": r((ix.iloc[-1] / ix.iloc[-2] - 1) * 100, 2),
                   "nepse_date": str(ix.index[-1]), "nepse_5d": r((ix.iloc[-1] / ix.iloc[-6] - 1) * 100, 2)})

    if write_log:
        update_picks_log(last_s, picks, avoid)
    evidence["live"] = live_record(p)
    evidence["base_hit"] = {k: r(v, 0) for k, v in base_hit.items()}
    evidence["halves"] = {"h1": f"{STUDY_FROM[:4]}–{int(SPLIT[:4]) - 1}", "h2": f"{SPLIT[:4]}–{last_s[:4]}"}

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_date": last_s,
        "regime": regime,
        "picks": sugg(picks, True), "avoid": sugg(avoid, False), "leaders": leaders_out,
        "setups": setups,
        "events": events,
        "stocks": stocks,
        "sectors": sec_rows,
        "rrg": rr,
        "model": evidence,
        "pick_liq_min": PICK_LIQ_MIN / 1e5,
        "tt_rules": TT_RULES,
    }
    data_dir = OUT_DIR / "data"
    (data_dir / "stocks").mkdir(parents=True, exist_ok=True)
    (data_dir / "screener.json").write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False))

    # ------------------------------------------------ per-share detail files
    nepse = index["close"].astype(float)
    for sym, f in per_stock.items():
        if sym not in active:
            continue
        row = latest_rows[sym]
        whys = {"up": reasons(row, imp, sign, True, 4), "dn": reasons(row, imp, sign, False, 4)} if np.isfinite(row.ai_raw) else {"up": [], "dn": []}
        write_detail(data_dir / "stocks" / f"{sym}.json", sym, f, rows_of[sym], nepse, row, whys)
    size = (data_dir / "screener.json").stat().st_size / 1e6
    print(f"wrote screener.json ({size:.1f} MB) and {len(active)} stock files; "
          f"{len(picks)} picks, {len(avoid)} to review, {sum(len(s['syms']) for s in setups)} setups today ({time.time() - t0:.0f}s)")


def write_detail(path, sym, f, prow, nepse, row, whys):
    t = f.tail(DETAIL_DAYS).reset_index(drop=True)
    ai = prow.set_index(prow.date.dt.strftime("%Y-%m-%d")).ai_pct.reindex(t.date)
    nx = nepse.reindex(t.date).ffill()
    rel = t.c / nx
    rel = rel / rel.dropna().iloc[0] * 100 if rel.notna().any() else rel
    evs = []
    for code in EVENTS:
        col = "ev_" + code
        if col in t:
            evs += [[int(i), code] for i in np.flatnonzero(t[col].astype(bool).to_numpy())]
    sup, res = support_resistance(f)
    rules = tt_rules(f.tail(1))
    tt = [bool(rules[k].iloc[0]) for k in range(7)] + [bool(row.rs_rating >= 70)]
    last = f.iloc[-1]
    doc = {
        "s": sym, "sec": row.sector,
        "d": t.date.tolist(), "o": rlist(t.o), "h": rlist(t.h), "l": rlist(t.l), "c": rlist(t.c),
        "v": rlist(t.amt / 1e5, 1),
        "m20": rlist(t.sma20), "m50": rlist(t.sma50), "m200": rlist(t.sma200),
        "st": rlist(t.st), "sd": [int(x) for x in t.st_dir],
        "bu": rlist(t.bb_up), "bl": rlist(t.bb_dn),
        "rsi": rlist(t.rsi14, 1), "macd": rlist(t.macd, 3), "sig": rlist(t.macd_sig, 3),
        "ai": [None if not np.isfinite(x) else int(round(x * 98 + 1)) for x in ai.to_numpy(dtype=float)],
        "rel": rlist(rel, 2),
        "ev": evs, "sup": sup, "res": res, "tt": tt,
        "stage": int(last.stage), "atr": r(last.atr14), "stop": r(last.st) if last.st_dir == 1 else None,
        "hi52": r(last.hi250), "lo52": r(last.lo250), "why": whys,
    }
    path.write_text(json.dumps(doc, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", action="store_true", help="upsert today's suggestions into data/picks-log.csv")
    main(ap.parse_args().log)
