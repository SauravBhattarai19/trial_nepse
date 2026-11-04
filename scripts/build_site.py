"""Build the interactive GitHub Pages site into _site/.

Copies the static app from site/, then writes:
  _site/data/market.json        NEPSE index OHLC + per-security-type daily
                                advancers/decliners/unchanged counts, so the
                                page can include/exclude mutual funds and
                                debentures and recompute the AD line itself
  _site/data/company-wise/*.csv the raw company files, loaded on demand when
                                a company is picked in the chart

Run after fetch_nepse_index.py (it reads data/nepse-index.csv).
"""
import datetime as dt
import json
import math
import shutil
from pathlib import Path

import pandas as pd

from securities import COMPANY_DIR, TYPES, load_companies

ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = ROOT / "site"
OUT_DIR = ROOT / "_site"
INDEX_PATH = ROOT / "data" / "nepse-index.csv"


def num(value, digits=2):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def main():
    index = pd.read_csv(INDEX_PATH).set_index("published_date")
    index = index[~index.index.duplicated(keep="last")]

    # counts[type][date] = [advancers, decliners, unchanged]
    counts = {kind: {} for kind in TYPES}
    securities = []
    for symbol, kind, df in load_companies():
        securities.append([symbol, kind, str(df["published_date"].iloc[-1])])
        per_type = counts[kind]
        for date, change in zip(df["published_date"].astype(str), df["per_change"]):
            row = per_type.setdefault(date, [0, 0, 0])
            # same rule as build_breadth.py: NaN (first row) counts as unchanged
            row[0 if change > 0 else 1 if change < 0 else 2] += 1

    # Timeline starts at the first NEPSE index day (1997); breadth before it
    # is a single security and only adds an empty stretch to the chart.
    start = index.index.min()
    dates = sorted(
        d for d in set(index.index).union(*(c.keys() for c in counts.values()))
        if d >= start
    )

    def col(name):
        series = index[name].reindex(dates)
        return [num(v) for v in series]

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_date": dates[-1],
        "dates": dates,
        "index": {
            "o": col("open"), "h": col("high"), "l": col("low"),
            "c": col("close"), "t": col("turnover"),
        },
        "breadth": {
            kind: {
                key: [counts[kind].get(d, (0, 0, 0))[i] for d in dates]
                for i, key in enumerate(("a", "d", "u"))
            }
            for kind in TYPES
        },
        "securities": securities,
    }

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(SITE_SRC, OUT_DIR)
    data_dir = OUT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "market.json").write_text(json.dumps(payload, separators=(",", ":")))
    shutil.copytree(COMPANY_DIR, data_dir / "company-wise")
    (OUT_DIR / ".nojekyll").touch()

    by_type = {k: sum(1 for s in securities if s[1] == k) for k in TYPES}
    print(f"Built {OUT_DIR}: {len(dates)} days ({dates[0]}..{dates[-1]}), securities {by_type}")


if __name__ == "__main__":
    main()
