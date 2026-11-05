"""Remove duplicate dates from data/company-wise/*.csv and put rows in date order.

One-off repair (idempotent; safe to rerun). In Nov 2026, 122 files had a date
listed twice, in three ways:

  * an identical row repeated (e.g. 2023-11-06/07, re-appended after 11-07 in
    104 files): one copy is kept;
  * the same session twice with only `per_change` differing (2012-2016, one of
    the two often a placeholder 0): the row whose `per_change` matches the move
    from the previous close is kept;
  * two different sessions filed under one date (2006-2011): the later row in
    the file is kept, so the closing-price path stays continuous (the next
    day's open follows it) and the two moves merge into one.

Lines are edited as text, so untouched rows keep their exact bytes.

    python scripts/clean_company_csvs.py           # repair in place
    python scripts/clean_company_csvs.py --check   # report only; exit 1 if anything needs fixing
"""
import argparse
import re
import sys
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent / "data" / "company-wise"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def num(s):
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v else None


def pick(group, prev_close, col):
    """Choose one line of a same-date group: list of (file_index, line, fields)."""
    lines = {g[1] for g in group}
    if len(lines) == 1:
        return group[0]
    pc = col["per_change"]
    same_but_change = all(
        [v for i, v in enumerate(g[2]) if i != pc] == [v for i, v in enumerate(group[0][2]) if i != pc] for g in group
    )
    if same_but_change and prev_close:
        close = num(group[0][2][col["close"]])
        if close is not None:
            move = 100 * (close / prev_close - 1)
            # closest per_change to the real move; ties go to the later line
            return min(reversed(group), key=lambda g: abs((num(g[2][pc]) if num(g[2][pc]) is not None else 1e9) - move))
    return group[-1]


def clean(text):
    """Return (new_text, removed_rows, reordered) for one CSV's text."""
    nl = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    raw = text.splitlines()
    head, body = raw[0], [ln for ln in raw[1:] if ln.strip()]
    col = {name: i for i, name in enumerate(head.split(","))}
    rows = []
    for k, ln in enumerate(body):
        f = ln.split(",")
        if not DATE.match(f[col["published_date"]]):
            raise ValueError(f"unexpected date {f[col['published_date']]!r}")
        rows.append((k, ln, f))
    dates = [r[2][col["published_date"]] for r in rows]
    reordered = any(b < a for a, b in zip(dates, dates[1:]))
    rows.sort(key=lambda r: r[2][col["published_date"]])          # stable: file order kept within a date
    out, prev_close, i = [], None, 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][2][col["published_date"]] == rows[i][2][col["published_date"]]:
            j += 1
        keep = pick(rows[i:j + 1], prev_close, col) if j > i else rows[i]
        out.append(keep[1])
        c = num(keep[2][col["close"]])
        if c and c > 0:
            prev_close = c
        i = j + 1
    removed = len(body) - len(out)
    new = nl.join([head] + out) + (nl if trailing else "")
    return new, removed, reordered


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    args = ap.parse_args()
    files = removed = reordered = 0
    for path in sorted(COMPANY_DIR.glob("*.csv")):
        text = path.read_text()
        new, n, moved = clean(text)
        if new != text:
            files += 1
            removed += n
            reordered += moved
            if not args.check:
                path.write_text(new)
    verb = "need fixing" if args.check else "fixed"
    print(f"{files} files {verb}: {removed} duplicate rows, {reordered} files out of date order")
    if args.check and files:
        sys.exit(1)


if __name__ == "__main__":
    main()
