"""One definition of "how much did this security move today", used everywhere
breadth is counted (build_breadth.py, build_site.py, signal_study.py).

Why not just use the CSV's per_change column?
  * Up to 2018-02-18 it is unreliable: many rows hold a placeholder 0 although
    the price moved (up to ~27% of rows in 2016-17), so those days were
    counted as "unchanged". For that era we use the close-to-close change
    instead, and skip moves beyond the then +-10% daily limit: those are
    bonus/rights book-closure adjustments, not trading.
  * From 2018-02-19 on, ShareSansar's per_change is measured against the
    ADJUSTED previous close (e.g. UPPER 2023-08-13: close -34% after a bonus
    issue, per_change +5.16%), so it is used as is. Note the daily limit is
    no longer a usable filter: since 2026 moves of +-15% are routine.
  * A security's first row has no previous price (listing day): skipped.

effective_change() returns a float Series aligned with the input rows; NaN
means "no usable reading today" and such rows are left out of breadth counts
(they are not "unchanged").
"""
import numpy as np
import pandas as pd

LEGACY_END = "2018-02-18"   # last date whose per_change can't be trusted
LEGACY_LIMIT = 10.5         # daily limit of that era (+-10%), plus rounding slack


def effective_change(df: pd.DataFrame) -> pd.Series:
    """df: one security's rows sorted by date, with published_date, close, per_change."""
    close = pd.to_numeric(df["close"], errors="coerce")
    close = close.where(close > 0)
    ret = (close / close.shift(1) - 1) * 100
    pc = pd.to_numeric(df["per_change"], errors="coerce")
    pc = pc.where(np.isfinite(pc))
    legacy = df["published_date"].astype(str) <= LEGACY_END
    eff = pc.where(~legacy, ret.where(ret.abs() <= LEGACY_LIMIT))
    eff = eff.where(close.notna())
    if len(eff):
        eff.iloc[0] = np.nan
    return eff
