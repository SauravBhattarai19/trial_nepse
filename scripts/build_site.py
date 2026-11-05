"""Build the interactive GitHub Pages site into _site/.

Copies the static app from site/, then writes:
  _site/data/market.json        NEPSE index OHLC + daily breadth readings per
                                security type and per sector (see the payload
                                comments below), so the page can choose which
                                securities count and recompute everything itself
  _site/data/company-wise/*.csv the raw company files, loaded on demand when
                                a company is picked in the chart

Run after fetch_nepse_index.py (it reads data/nepse-index.csv).
"""
import datetime as dt
import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from price_changes import effective_change
from sectors import UNCLASSIFIED, load_sectors
from securities import COMPANY_DIR, EQUITY, TYPES, load_companies

ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = ROOT / "site"
OUT_DIR = ROOT / "_site"
INDEX_PATH = ROOT / "data" / "nepse-index.csv"


def num(value, digits=2):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


SECTOR_START = "2011-01-01"   # sector arrays start here (fewer than ~20 shares traded before)
LISTING_DAYS = 20             # leave a share's first 20 trading days out of equal-weight returns:
                              # post-IPO runs of daily limit-up moves would swamp the "average stock"
INT_KEYS = ("a", "d", "u", "ab", "el", "hi", "lo", "ne")
FLOAT_KEYS = {"ut": 1, "dt": 1, "se": 2}   # rounding digits


def security_series(df):
    """Per-row readings for one security (rows sorted by date).

    eff        effective % move (price_changes.effective_change; NaN = no reading)
    eff_ew     the same without the first LISTING_DAYS rows, for equal-weight returns
    level      bonus/rights-adjusted price path (product of 1 + eff), for the
               50-day-MA and 52-week high/low readings, so a book-closure day
               doesn't look like a crash
    """
    eff = effective_change(df).to_numpy(dtype=float)
    level = pd.Series(np.cumprod(1 + np.nan_to_num(eff) / 100))
    ma50 = level.rolling(50).mean().to_numpy()
    hi250 = level.rolling(250).max().to_numpy()
    lo250 = level.rolling(250).min().to_numpy()
    lv = level.to_numpy()
    with np.errstate(invalid="ignore"):
        eff_ew = eff.copy()
        eff_ew[:LISTING_DAYS] = np.nan
        return {
            "eff": eff,
            "eff_ew": eff_ew,
            "amount": pd.to_numeric(df["traded_amount"], errors="coerce").to_numpy(dtype=float),
            "el": ~np.isnan(ma50),
            "ab": lv > ma50,
            "hi": lv >= hi250,
            "lo": lv <= lo250,
        }


def empty_group(n):
    return {k: np.zeros(n, dtype=np.int64) for k in INT_KEYS} | {k: np.zeros(n) for k in FLOAT_KEYS}


def add_to_group(g, pos, r):
    """Accumulate one security's rows (at date positions `pos`) into group g."""
    eff, ok = r["eff"], ~np.isnan(r["eff"])
    up, dn, flat = ok & (eff > 0), ok & (eff < 0), ok & (eff == 0)
    amt = np.nan_to_num(r["amount"])
    np.add.at(g["a"], pos[up], 1)
    np.add.at(g["d"], pos[dn], 1)
    np.add.at(g["u"], pos[flat], 1)
    np.add.at(g["ut"], pos[up], amt[up] / 1e5)       # NPR lakh
    np.add.at(g["dt"], pos[dn], amt[dn] / 1e5)
    ew = r["eff_ew"]
    ew_ok = ~np.isnan(ew)
    np.add.at(g["se"], pos[ew_ok], ew[ew_ok])         # equal-weight average = se / ne
    np.add.at(g["ne"], pos[ew_ok], 1)
    np.add.at(g["el"], pos[r["el"]], 1)
    np.add.at(g["ab"], pos[r["el"] & r["ab"]], 1)
    np.add.at(g["hi"], pos[r["hi"]], 1)
    np.add.at(g["lo"], pos[r["lo"]], 1)


def serialize(g, offset=0):
    out = {k: g[k][offset:].tolist() for k in INT_KEYS}
    out.update({k: np.round(g[k][offset:], d).tolist() for k, d in FLOAT_KEYS.items()})
    return out


def stamp_assets(out_dir: Path):
    """Point index.html at app.js / style.css with a ?v=<content hash> suffix.

    GitHub Pages lets browsers cache each file for a while; without this a
    visitor can get a fresh index.html with yesterday's app.js (or the other
    way round), which looks like the "old site" or breaks the page.
    """
    page = out_dir / "index.html"
    html = page.read_text()
    for name in ("style.css", "app.js", "screener.js", "vendor/lightweight-charts.standalone.production.js"):
        digest = hashlib.sha1((out_dir / name).read_bytes()).hexdigest()[:10]
        for attr in ("href", "src"):
            html = html.replace(f'{attr}="{name}"', f'{attr}="{name}?v={digest}"')
    page.write_text(html)


def main():
    index = pd.read_csv(INDEX_PATH).set_index("published_date")
    index = index[~index.index.duplicated(keep="last")]
    sector_of = load_sectors()

    companies = list(load_companies())
    # Timeline starts at the first NEPSE index day (1997); breadth before it
    # is a single security and only adds an empty stretch to the chart.
    start = index.index.min()
    all_dates = set(index.index)
    for _, _, df in companies:
        all_dates.update(df["published_date"].astype(str))
    dates = sorted(d for d in all_dates if d >= start)
    position = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    types = {kind: empty_group(n) for kind in TYPES}
    sectors = {}
    securities = []
    for symbol, kind, df in companies:
        sector = sector_of.get(symbol, UNCLASSIFIED) if kind == EQUITY else ""
        securities.append([symbol, kind, str(df["published_date"].iloc[-1]), sector])
        r = security_series(df)
        pos = np.array([position.get(d, -1) for d in df["published_date"].astype(str)])
        keep = pos >= 0
        pos, r = pos[keep], {k: v[keep] for k, v in r.items()}
        add_to_group(types[kind], pos, r)
        if kind == EQUITY:
            add_to_group(sectors.setdefault(sector, empty_group(n)), pos, r)

    def col(name):
        series = index[name].reindex(dates)
        return [num(v) for v in series]

    sector_offset = next(i for i, d in enumerate(dates) if d >= SECTOR_START)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_date": dates[-1],
        "dates": dates,
        "index": {
            "o": col("open"), "h": col("high"), "l": col("low"),
            "c": col("close"), "t": col("turnover"),
        },
        # Per security type, one value per date:
        #   a/d/u   advancer / decliner / unchanged counts
        #   ut/dt   turnover (NPR lakh) in advancing / declining securities (money flow)
        #   el/ab   securities with a 50-day MA / of those, trading above it
        #   hi/lo   securities at a 52-week (250 trading day) high / low
        #   se/ne   sum and count of daily % moves (equal-weight average = se/ne),
        #           excluding each share's first LISTING_DAYS trading days
        "breadth": {kind: serialize(g) for kind, g in types.items()},
        # Same arrays per sector of ordinary shares, starting at dates[sector_offset]
        "sector_offset": sector_offset,
        "sectors": {name: serialize(g, sector_offset) for name, g in sorted(sectors.items())},
        "securities": securities,
    }

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(SITE_SRC, OUT_DIR)
    stamp_assets(OUT_DIR)
    data_dir = OUT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "market.json").write_text(json.dumps(payload, separators=(",", ":")))
    shutil.copytree(COMPANY_DIR, data_dir / "company-wise")
    (OUT_DIR / ".nojekyll").touch()

    by_type = {k: sum(1 for s in securities if s[1] == k) for k in TYPES}
    print(f"Built {OUT_DIR}: {len(dates)} days ({dates[0]}..{dates[-1]}), securities {by_type}, "
          f"{len(sectors)} sectors, market.json {(data_dir / 'market.json').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
