# import required library and required constants
import time
import requests
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup
from utils.status import getStatus
from constants.url import dailyPriceUrl

html = requests.get(dailyPriceUrl).text
bs = BeautifulSoup(html, "lxml")

# today date in yyyy-mm-dd format
today = bs.find("span", {"class": "text-org"}).text

# get html tables
tables = pd.read_html(html)

# select the first table i.e. the stock price table
dataTable = tables[0]

fileDir = Path("../data/company-wise/")
for file in fileDir.glob("*.csv"):
    symbol = str(file).split(".")[2].split("/")[-1]
    data = dataTable.loc[dataTable["Symbol"] == symbol]
    if len(data) != 1:
        continue
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
