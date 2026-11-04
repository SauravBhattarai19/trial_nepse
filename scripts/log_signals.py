"""Append today's breadth readings to data/signal-log.csv (forward-test record).

The backtests in signal_study.py are hindsight; this log is not. Each run adds
the latest trading day's score, its five components and any AD-line crossover,
or refreshes that row if the day is already logged (later scraper runs can
correct prices). Earlier rows are never rewritten, so after a few months the
file is an honest out-of-sample record of what the signals said at the time.

Uses the same definitions as signal_study.py (ordinary shares only).
"""
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_study import build_features, load_panel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "signal-log.csv"
FIELDS = [
    "date", "nepse_close", "advancers", "decliners", "ad_line", "ad_ma10", "ad_ma20",
    "ad_above_ma10", "ma10_above_ma20", "ad_x_ma10", "ma10_x_ma20",
    "mcclellan_19_39", "summation_5d_change", "pct_advancing_10d", "money_flow_10d",
    "score", "logged_at_utc",
]


def cross(fast, slow, i):
    """+1 bullish cross on row i, -1 bearish, 0 none."""
    now, before = fast.iloc[i] > slow.iloc[i], fast.iloc[i - 1] > slow.iloc[i - 1]
    return 0 if now == before else (1 if now else -1)


def main():
    p = load_panel()
    f, _ = build_features(p)
    ma10, ma20 = f.ad.rolling(10).mean(), f.ad.rolling(20).mean()
    i = len(f) - 1
    day = f.index[i]
    row = {
        "date": day.strftime("%Y-%m-%d"),
        "nepse_close": round(float(f.C.iloc[i]), 2),
        "advancers": int(p.A.loc[day]),
        "decliners": int(p.D.loc[day]),
        "ad_line": int(f.ad.iloc[i]),
        "ad_ma10": round(float(ma10.iloc[i]), 1),
        "ad_ma20": round(float(ma20.iloc[i]), 1),
        "ad_above_ma10": int(f.ad.iloc[i] > ma10.iloc[i]),
        "ma10_above_ma20": int(ma10.iloc[i] > ma20.iloc[i]),
        "ad_x_ma10": cross(f.ad, ma10, i),
        "ma10_x_ma20": cross(ma10, ma20, i),
        "mcclellan_19_39": round(float(f.mcclellan.iloc[i]), 1),
        "summation_5d_change": round(float(f.summation.iloc[i] - f.summation.iloc[i - 5]), 1),
        "pct_advancing_10d": round(float(f.pct_adv10.iloc[i]) * 100, 1),
        "money_flow_10d": round(float(f.money_flow10.iloc[i]) * 100, 1),
        "score": int(f.score.iloc[i]),
        "logged_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    rows = []
    if LOG_PATH.exists():
        with LOG_PATH.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    if rows and rows[-1]["date"] > row["date"]:
        print(f"Log already has {rows[-1]['date']}, newer than {row['date']}; nothing to do.")
        return
    if rows and rows[-1]["date"] == row["date"]:
        rows[-1] = row
    else:
        rows.append(row)
    with LOG_PATH.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Logged {row['date']}: score {row['score']}/5, AD x MA10 {row['ad_x_ma10']:+d}, MA10 x MA20 {row['ma10_x_ma20']:+d}")


if __name__ == "__main__":
    main()
