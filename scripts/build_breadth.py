"""Aggregate per-company daily prices into market-wide advancers/decliners counts.

Reads data/company-wise/*.csv and writes data/market-breadth.csv with one row
per trading day: advancers, decliners, unchanged, total_traded.

A security's daily move comes from price_changes.effective_change(): it
repairs the placeholder per_change values before 2018-02-19 and skips rows
with no usable reading (listing day, bonus/rights adjustments of that era),
so those don't inflate the "unchanged" count.
"""
from collections import defaultdict
from pathlib import Path

from price_changes import effective_change
from securities import load_companies

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "market-breadth.csv"


def main():
    counts = defaultdict(lambda: [0, 0, 0])
    for _, _, df in load_companies():
        for date, chg in zip(df["published_date"].astype(str), effective_change(df)):
            if chg != chg:          # NaN: no usable reading today
                continue
            counts[date][0 if chg > 0 else 1 if chg < 0 else 2] += 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="") as f:
        f.write("date,advancers,decliners,unchanged,total_traded\n")
        for date in sorted(counts):
            a, d, u = counts[date]
            f.write(f"{date},{a},{d},{u},{a + d + u}\n")
    print(f"Wrote {len(counts)} trading days -> {OUT_PATH}")


if __name__ == "__main__":
    main()
