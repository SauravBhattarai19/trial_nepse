"""Which market-breadth signals actually say something about NEPSE's next move?

Reproducible research behind the "Insights" section of the site. Prints a
plain-text report; nothing is written to disk. Run from anywhere:

    python scripts/signal_study.py

Method
  * Breadth uses ordinary shares only (mutual funds and debentures removed;
    including them changes the results by < 0.01 in rank correlation).
  * Study window 2013-01-01 .. latest. Before 2013 fewer than ~45 shares
    traded on a typical day, too few for breadth to mean much.
  * Every result is shown for two periods: 2013-2019 ("in-sample") and
    2020-latest ("out-of-sample"). Anything that works in one and not the
    other is treated as unproven.
  * Forward returns are TRADEABLE: a signal seen at day t's close is acted on
    at day t+1's open (NEPSE's opening gap already contains most of the
    previous day's follow-through, so close-to-close returns overstate any
    edge). r_h = close(t+h) / open(t+1) - 1.
  * Backtests are long/cash on the NEPSE index with 0.4% cost per side.
    The index itself cannot be bought, so treat them as indicative.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from securities import EQUITY, load_companies  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
START, SPLIT = "2013-01-01", "2020-01-01"
COST = 0.004
rng = np.random.default_rng(0)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def load_panel():
    frames = []
    for _, kind, df in load_companies():
        if kind == EQUITY:
            frames.append(df[["published_date", "close", "per_change", "traded_amount"]])
    c = pd.concat(frames)
    c = c[c.close > 0]
    c["published_date"] = pd.to_datetime(c["published_date"])
    up, dn = c.per_change > 0, c.per_change < 0
    breadth = pd.DataFrame({
        "A": up.groupby(c.published_date).sum(),
        "D": dn.groupby(c.published_date).sum(),
        "UT": c.traded_amount.where(up, 0).groupby(c.published_date).sum(),
        "DT": c.traded_amount.where(dn, 0).groupby(c.published_date).sum(),
    })
    ix = pd.read_csv(ROOT / "data" / "nepse-index.csv", parse_dates=["published_date"])
    ix = ix.drop_duplicates("published_date", keep="last").set_index("published_date")
    p = ix[["open", "close", "turnover"]].join(breadth, how="inner")
    return p[p.index >= "2011-01-01"]


def build_features(p):
    f = pd.DataFrame(index=p.index)
    C, O = p.close, p.open
    f["r1"] = C.pct_change()
    f["gap_next"] = (O / C.shift(1) - 1).shift(-1)
    f["intra_next"] = (C / O - 1).shift(-1)
    for h in (1, 5, 20, 60):
        f[f"t{h}"] = C.shift(-h) / O.shift(-1) - 1
    tot = (p.A + p.D).replace(0, np.nan)
    f["ad"] = (p.A - p.D).cumsum()                       # Advance/Decline line
    f["pct_adv"] = p.A / tot
    f["net_ratio"] = (p.A - p.D) / tot
    f["mcclellan"] = ema(f.net_ratio * 1000, 19) - ema(f.net_ratio * 1000, 39)
    f["summation"] = f.mcclellan.cumsum()
    f["pct_adv10"] = ema(f.pct_adv, 10)
    f["money_flow10"] = ema(p.UT / (p.UT + p.DT).replace(0, np.nan), 10)
    f["vol_ratio"] = p.turnover / p.turnover.rolling(20).mean()
    f["mom20"] = C / C.shift(20) - 1
    f["ad_dev10"] = f.ad - f.ad.rolling(10).mean()
    f["C"] = C
    comp = pd.DataFrame({
        "AD line > its 10-day MA": f.ad > f.ad.rolling(10).mean(),
        "McClellan oscillator > 0": f.mcclellan > 0,
        "10-day % advancing > 50%": f.pct_adv10 > 0.5,
        "10-day money flow > 50%": f.money_flow10 > 0.5,
        "Summation index rising (5d)": f.summation.diff(5) > 0,
    })
    f["score"] = comp.sum(axis=1)
    return f[f.index >= START], list(comp.columns)


def rank_corr(a, b):
    m = a.notna() & b.notna()
    return a[m].rank().corr(b[m].rank())


def backtest(pos, f, period):
    """Long/cash on the index: decide at close t, trade at open t+1."""
    gap = f.gap_next.shift(1).fillna(0)                # close(t-1) -> open(t)
    intra = f.intra_next.shift(1).fillna(0)            # open->close of day t
    pos = pos.astype(float).fillna(0)
    session, overnight = pos.shift(1).fillna(0), pos.shift(2).fillna(0)
    r = (1 + overnight * gap) * (1 + session * intra) - 1 - session.diff().abs().fillna(0) * COST
    r = r[period]
    eq = (1 + r).cumprod()
    years = len(r) / 240
    return {
        "CAGR %": (eq.iloc[-1] ** (1 / years) - 1) * 100,
        "max DD %": (eq / eq.cummax() - 1).min() * 100,
        "Sharpe": r.mean() / r.std() * np.sqrt(240),
        "invested %": session[period].mean() * 100,
    }


def crosses(a, b):
    state = (a > b).astype(float).where(a.notna() & b.notna())
    prev = state.shift(1)
    return (state == 1) & (prev == 0), (state == 0) & (prev == 1)


def main():
    p = load_panel()
    f, components = build_features(p)
    periods = {"2013-19": f.index < SPLIT, f"2020-{f.index.max():%y}": f.index >= SPLIT}
    pd.set_option("display.width", 200)

    print(f"Breadth signal study, {f.index.min():%Y-%m-%d} .. {f.index.max():%Y-%m-%d}\n")

    print("1. What moves NEPSE on the day (correlation of daily index return with breadth)")
    for lab, m in periods.items():
        print(f"   {lab}: net % advancing {f.r1[m].corr(f.net_ratio[m]):.3f}   "
              f"(R^2 {f.r1[m].corr(f.net_ratio[m]) ** 2:.2f})")

    print("\n2. Next-day follow-through: corr(today's return, tomorrow's opening gap / open->close)")
    for lab, m in periods.items():
        print(f"   {lab}: gap {f.r1[m].corr(f.gap_next[m]):.3f}   rest of day {f.r1[m].corr(f.intra_next[m]):.3f}")

    print("\n3. Rank correlation of each reading with the TRADEABLE forward return")
    rows = []
    for x in ("money_flow10", "pct_adv10", "mcclellan", "ad_dev10", "net_ratio", "vol_ratio", "mom20", "score"):
        row = {"reading": x}
        for lab, m in periods.items():
            for h in (5, 20, 60):
                row[f"{lab} {h}d"] = round(rank_corr(f[x][m], f[f"t{h}"][m]), 3)
        rows.append(row)
    print(pd.DataFrame(rows).set_index("reading").to_string())

    print("\n4. Next 5 / 20 trading days by breadth score (0 = all five bearish, 5 = all bullish)")
    print("   components: " + "; ".join(components))
    for lab, m in periods.items():
        t = f[m].groupby("score").agg(days=("t5", "size"), avg5=("t5", "mean"), up5=("t5", lambda x: (x > 0).mean()),
                                      avg20=("t20", "mean"), up20=("t20", lambda x: (x > 0).mean()))
        t[["avg5", "avg20"]] *= 100
        t[["up5", "up20"]] *= 100
        print(f"   {lab}\n" + t.round(2).to_string())

    print("\n5. AD-line crossovers: avg return after bullish cross minus after bearish cross (%), hit% up/down")
    ma = lambda s, n: s.rolling(n).mean()  # noqa: E731
    cases = {f"AD x MA{n}": (f.ad, ma(f.ad, n)) for n in (3, 5, 7, 10, 20, 50)}
    cases.update({f"MA{a} x MA{b}": (ma(f.ad, a), ma(f.ad, b)) for a, b in ((5, 10), (5, 20), (10, 20), (10, 50))})
    rows = []
    for name, (a, b) in cases.items():
        up, dn = crosses(a, b)
        row = {"cross": name}
        for lab, m in periods.items():
            for h in (5, 20):
                u, d = f[f"t{h}"][up & m].dropna(), f[f"t{h}"][dn & m].dropna()
                row[f"{lab} {h}d"] = f"{(u.mean() - d.mean()) * 100:+.2f} ({(u > 0).mean() * 100:.0f}/{(d > 0).mean() * 100:.0f})"
            row[f"{lab} n"] = int(up[m].sum())
        rows.append(row)
    print(pd.DataFrame(rows).set_index("cross").to_string())

    print("\n6. Divergences and extremes (avg tradeable return %, vs all days; p from 2000 random draws)")
    def first_in(ev, gap):
        keep, last = [], -10 ** 9
        for i in np.flatnonzero(ev.values):
            if i - last >= gap:
                keep.append(i)
                last = i
        out = pd.Series(False, index=ev.index)
        out.iloc[keep] = True
        return out
    ra = f.net_ratio.fillna(0).cumsum()
    newhi, adhi = f.C >= f.C.rolling(20).max(), ra >= ra.rolling(20).max()
    newlo, adlo = f.C <= f.C.rolling(20).min(), ra <= ra.rolling(20).min()
    low_recent = (f.pct_adv10 < 0.40).astype(int).rolling(10).max() == 1
    events = {
        "NEPSE 20d high, breadth not (bearish divergence)": first_in(newhi & ~adhi, 10),
        "NEPSE 20d high, breadth confirms": first_in(newhi & adhi, 10),
        "NEPSE 20d low, breadth confirms": first_in(newlo & adlo, 10),
        "Breadth thrust (10d %adv <40% -> >61.5%)": first_in((f.pct_adv10 > 0.615) & low_recent, 20),
        "Washout (10d %adv < 30%)": first_in(f.pct_adv10 < 0.30, 20),
    }
    for name, ev in events.items():
        line = f"   {name:<50}"
        for lab, m in periods.items():
            e, base = f.t20[ev & m].dropna(), f.t20[m].dropna()
            if len(e) < 4:
                line += f"| {lab} n={len(e)} "
                continue
            sims = np.array([rng.choice(base.values, len(e), replace=False).mean() for _ in range(2000)])
            pv = (np.abs(sims - base.mean()) >= abs(e.mean() - base.mean())).mean()
            line += f"| {lab} 20d n={len(e):<3} {e.mean() * 100:+5.2f} vs {base.mean() * 100:+5.2f} p={pv:.2f} "
        print(line)

    print("\n7. Long/cash backtests (enter next open, 0.4% per side)")
    rules = {
        "Buy & hold": pd.Series(True, index=f.index),
        "AD line > MA10": f.ad > ma(f.ad, 10),
        "AD MA10 > MA20": ma(f.ad, 10) > ma(f.ad, 20),
        "AD line > MA50": f.ad > ma(f.ad, 50),
        "NEPSE > its MA20": f.C > ma(f.C, 20),
        "Money flow10 > 50%": f.money_flow10 > 0.5,
        "Summation rising": f.summation.diff(5) > 0,
        "Score >= 3": f.score >= 3,
    }
    rows = []
    for name, pos in rules.items():
        row = {"rule": name}
        for lab, m in periods.items():
            for k, v in backtest(pos, f, m).items():
                row[f"{lab} {k}"] = round(v, 2 if k == "Sharpe" else 1)
        rows.append(row)
    print(pd.DataFrame(rows).set_index("rule").to_string())


if __name__ == "__main__":
    main()
