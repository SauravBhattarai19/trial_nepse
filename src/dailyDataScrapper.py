# import required library and required constants
import io
import re
import sys
import requests
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup
from utils.status import getStatus
from constants.url import dailyPriceUrl

response = requests.get(dailyPriceUrl, timeout=60)
response.raise_for_status()
html = response.text
bs = BeautifulSoup(html, "lxml")

# today date in yyyy-mm-dd format
dateSpan = bs.find("span", {"class": "text-org"})
today = dateSpan.text.strip() if dateSpan else ""
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
    sys.exit(f"Could not read the trading date from {dailyPriceUrl} (got {today!r}); the page layout may have changed.")

# get html tables
# (pandas >= 2.1 wants a file-like object, not a raw HTML string)
tables = pd.read_html(io.StringIO(html))

# select the first table i.e. the stock price table
dataTable = tables[0]

# Fail loudly instead of silently writing nothing if the page changes shape.
# On a normal day ~300 of our companies appear in the table.
MIN_MATCHED = 100
missingCols = {"Symbol", "Open", "High", "Low", "Close", "Diff %", "Vol", "Turnover"} - set(dataTable.columns)
if missingCols:
    sys.exit(f"today-share-price table is missing columns {sorted(missingCols)}; the page layout may have changed.")

matched = updated = 0
fileDir = Path("../data/company-wise/")
for file in fileDir.glob("*.csv"):
    symbol = str(file).split(".")[2].split("/")[-1]
    data = dataTable.loc[dataTable["Symbol"] == symbol]
    if len(data) != 1:
        continue
    matched += 1
    data = data.iloc[0]
    status = getStatus(float(data["Open"]), float(data["Close"]))
    dataRow = [
        [
            today,
            float(data["Open"]),
            float(data["High"]),
            float(data["Low"]),
            float(data["Close"]),
            float(data["Diff %"]),
            float(data["Vol"]),
            float(data["Turnover"]),
            status,
        ]
    ]
    newLine = pd.DataFrame(dataRow).to_csv(header=False, index=False)

    # The first run of the day happens right after the market closes. If
    # today's row is already on file, replace it rather than skipping, so a
    # later run picks up any late correction on the site. Re-running with
    # unchanged prices rewrites the same bytes, so it stays idempotent.
    lines = file.read_text().splitlines(keepends=True)
    lastDate = lines[-1].split(",")[0]
    if str(today) < str(lastDate):
        continue
    if str(lastDate) == str(today):
        if lines[-1] == newLine:
            continue
        lines[-1] = newLine
    else:
        lines.append(newLine)
    file.write_text("".join(lines))
    updated += 1

print(f"{today}: {matched} companies found on the page, {updated} files written.")
if matched < MIN_MATCHED:
    sys.exit(f"Only {matched} of our companies were found on {dailyPriceUrl} (expected {MIN_MATCHED}+); the page layout may have changed.")
