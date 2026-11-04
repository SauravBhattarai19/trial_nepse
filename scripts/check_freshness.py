"""Is our company data as fresh as ShareSansar's latest trading day?

Compares the date shown on ShareSansar's today-share-price page (the page the
daily scraper reads) with the newest date in data/company-wise. Comparing
against the site, not the calendar, keeps market holidays from raising false
alarms: on a holiday the site still shows the last trading day, which we have.

Prints a one-line summary and, under GitHub Actions, writes step outputs:
  status     ok | stale | unreachable
  site_date  date shown on ShareSansar ("" if unreachable)
  data_date  newest date in data/company-wise
Always exits 0; the workflow decides what to do.
"""
import os
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
COMPANY_DIR = ROOT / "data" / "company-wise"
URL = "https://www.sharesansar.com/today-share-price"
DATE_RE = re.compile(r'<span class="text-org">\s*(\d{4}-\d{2}-\d{2})\s*</span>')


def newest_data_date() -> str:
    newest = ""
    for path in COMPANY_DIR.glob("*.csv"):
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200))
            last = f.read().decode(errors="ignore").strip().splitlines()[-1]
        newest = max(newest, last.split(",")[0])
    return newest


def site_date() -> str:
    try:
        html = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"}).text
    except requests.RequestException as exc:
        print(f"Could not reach {URL}: {exc}")
        return ""
    m = DATE_RE.search(html)
    return m.group(1) if m else ""


def main():
    data_date = newest_data_date()
    shown = site_date()
    if not shown:
        status = "unreachable"
    elif shown > data_date:
        status = "stale"
    else:
        status = "ok"
    print(f"status={status} site_date={shown or '-'} data_date={data_date}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"status={status}\nsite_date={shown}\ndata_date={data_date}\n")


if __name__ == "__main__":
    main()
