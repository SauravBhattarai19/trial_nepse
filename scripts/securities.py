"""Classify each company-wise CSV as an equity, a mutual fund or a debenture.

NEPSE lists more than ordinary shares: closed-end mutual fund units (which
trade around their NPR 10 face value) and debentures/bonds (around their
NPR 1,000 face value) sit in the same price table. Their daily moves say
little about market breadth, so the interactive site lets you leave them out.

Classification is rule-based so newly listed securities are picked up
without anyone editing a list:

  debenture    symbol ends in D + a 2- or 4-digit maturity year (NBLD87,
               SBLD2091, ...) or is one of the explicit symbols below
  mutual_fund  one of the known funds below, or any security whose median
               closing price is under NPR 25 (no NEPSE equity trades
               anywhere near that; every fund sits around NPR 10)
  equity       everything else (including promoter shares)

`python scripts/securities.py` writes data/security-types.csv so the result
can be inspected.
"""
import csv
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
COMPANY_DIR = ROOT / "data" / "company-wise"
OUT_PATH = ROOT / "data" / "security-types.csv"

EQUITY = "equity"
MUTUAL_FUND = "mutual_fund"
DEBENTURE = "debenture"
TYPES = (EQUITY, MUTUAL_FUND, DEBENTURE)

KNOWN_MUTUAL_FUNDS = {
    "C30MF", "CMF1", "CMF2", "CSY", "GBIMESY2", "GIBF1", "GSY", "H8020",
    "HLICF", "KDBY", "KEF", "KSY", "LUK", "LVF2", "MBLEF", "MMF1", "MNMF1",
    "NBF2", "NBF3", "NIBLGF", "NIBLSTF", "NIBSF2", "NICBF", "NICFC",
    "NICGF2", "NICSF", "NMB50", "NMBHF2", "NSIF2", "NSY", "PRSF", "PSF",
    "RBBF40", "RMF1", "RMF2", "RSY", "SAGF", "SBCF", "SEF", "SFEF", "SFMF",
    "SIGS2", "SIGS3", "SLCF",
}
KNOWN_DEBENTURES = {"SCBD", "SHINED", "NIFRAGED"}
DEBENTURE_PATTERN = re.compile(r"D(?:\d{2}|20\d{2})$")
MUTUAL_FUND_MAX_MEDIAN_CLOSE = 25.0


def classify(symbol: str, closes: pd.Series) -> str:
    if symbol in KNOWN_DEBENTURES or DEBENTURE_PATTERN.search(symbol):
        return DEBENTURE
    if symbol in KNOWN_MUTUAL_FUNDS:
        return MUTUAL_FUND
    median = closes.median()
    if pd.notna(median) and median < MUTUAL_FUND_MAX_MEDIAN_CLOSE:
        return MUTUAL_FUND
    return EQUITY


def load_companies():
    """Yield (symbol, type, dataframe) for every company-wise CSV."""
    for path in sorted(COMPANY_DIR.glob("*.csv")):
        df = pd.read_csv(path)
        yield path.stem, classify(path.stem, df["close"]), df


def main():
    with OUT_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "type"])
        counts = dict.fromkeys(TYPES, 0)
        for symbol, kind, _ in load_companies():
            writer.writerow([symbol, kind])
            counts[kind] += 1
    print(f"Wrote {OUT_PATH}: {counts}")


if __name__ == "__main__":
    main()
