# nepse-data

nepse-data is the collection of all the datasets (from past to present) of various companies listed in the Nepal Stock Market. This repository is built for the students and people who want to perform analysis on Nepal Stock Market using historical (& current) data.

## Data

The data can be found in the [data/company-wise](https://github.com/Aabishkar2/nepse-data/tree/main/data/company-wise) folder, one CSV per company, named by its trading symbol. For example: in `NMB.csv` you can find all the data of NMB Bank Limited sorted in ascending order (date-wise).

The repository currently includes data for around 370 companies, with history going back as far as 1995. Feel free to create an issue if you want data of any particular company urgently.

Each CSV has the following columns:

| Column | Description |
| --- | --- |
| `published_date` | Trading date (`YYYY-MM-DD`) |
| `open` | Opening price |
| `high` | Day's high |
| `low` | Day's low |
| `close` | Closing price |
| `per_change` | Percentage change from the previous close (`nan` for the first row of a series) |
| `traded_quantity` | Number of shares traded |
| `traded_amount` | Total turnover for the day |
| `status` | Derived: `1` if close > open, `-1` if open > close, `0` if equal |

The Github Actions updates the data on an almost daily basis so that the datasets available here are up to date.

## Code

The code through which the data were/are being collected resides in the [src/](https://github.com/Aabishkar2/nepse-data/tree/main/src) folder. The code is written in `python3.8` and the required libraries are listed in `src/requirements.txt`.

If you want to collect the datasets by yourself then you can run the following commands.

Firstly, Just make sure you have `python3` installed 😉. If not please find python3 installation procedure over [here](https://www.python.org/downloads/)

```bash
# go into the src folder -- the scrapers must be run from here
cd src

# install requirements
pip3 install -r requirements.txt

# refresh the symbol -> company-id map from the live site
# (run this before a backfill so newly listed companies are picked up)
python3 discoverCompanies.py

# for historical data collection
# this only seeds companies that don't have a CSV yet -- existing files are left alone
python3 allDataScrapper.py

# optional -- for daily data updates
python3 dailyDataScrapper.py
```

A few things worth knowing:

- All three scripts import using paths relative to `src/`, so they only work when run from inside that directory.
- `discoverCompanies.py` regenerates `src/constants/companyIdMap.py`. That file is generated — don't hand-edit it. It merges with the existing map, so delisted companies are never dropped.
- `allDataScrapper.py` skips any symbol that already has a CSV, so re-running it won't clobber or duplicate existing data.
- `dailyDataScrapper.py` only appends to CSVs that already exist and is idempotent — running it twice in a day won't add a duplicate row.

The code that updates data on a daily basis resides in the `.github/workflows` directory and runs on Github Actions as a CRON job. The workflow runs 5 times every day (Sunday to Friday), starting at 15:15 Nepal time right after the market closes, so that if the data collection is missed the first time then it will work the second time and so on. A later run also refreshes today's row if the prices on the site changed.

## Interactive chart (GitHub Pages)

**Live: https://sauravbhattarai19.github.io/trial_nepse/**

A trading-style chart of the NEPSE index (candlesticks, volume, moving averages),
the Advance/Decline line and daily advancers vs decliners, with the three
panes scrolled, zoomed and crosshaired together (mouse wheel / pinch to zoom,
drag to pan). You can:

- turn **mutual funds** and **debentures** on or off: the breadth numbers,
  the AD line, the stat tiles and the CSV download are all recomputed
- pick the moving averages drawn on the price and AD-line panes: tap the
  3 / 5 / 7 / 10 / 20 / 50-day presets or type any number of days (2–1000)
  and press *Add*; up to 3 at once, calculated in the browser over trading
  days, and included in the CSV download
- switch the price pane from the NEPSE index to any company symbol
- jump to 1M / 3M / 6M / 1Y / 3Y / 5Y / All, and share a view by URL
  (e.g. `#mf=0&s=NABIL&r=3M&ma=3,5,7&p=flow&sig=ma:10:20&per=oos`)

- switch the lower pane between advancers/decliners, the **McClellan
  oscillator** and **money flow** (10-day share of turnover in rising stocks)
- read the **Breadth signals** section: a live 0–5 breadth score, what NEPSE
  did after each score historically, and a **crossover check** that marks
  AD-line crossovers (AD × MA *n*, or MA *a* × MA *b*) on the chart and shows
  NEPSE's average move and hit rate over the next 5 and 20 days, for
  2013–2019 vs since 2020

### What the data says (study of 2013 – Nov 2026)

`python scripts/signal_study.py` reproduces all of this. Each result is checked
on 2013–2019 and on 2020–2026 separately, with returns measured from the
*next* day's open (NEPSE's opening gap already absorbs most of the previous
day's follow-through, so close-to-close tests overstate any edge).

- **The market decider:** day to day, NEPSE goes where most stocks go. Net %
  advancing explains ~77% of daily index moves. Looking ahead, the best single
  guide is *money flow*, the 10-day share of turnover in rising stocks. It beat
  the index's own momentum at predicting the next week (rank correlation 0.19 /
  0.12 vs 0.16 / 0.03).
- **Crossovers that held up in both periods:** AD line × its 10-day MA
  (bullish crosses beat bearish ones by ~0.8% over the next week) and the AD
  line's 10-day × 20-day MA (1.9% / 3.2% over the next 20 days). The 50-day MA
  crosses stopped working after 2020. 10 and 20 are now the default lines.
- **Bearish signals are the louder warning since 2020:** after the AD line falls
  below its 7-day MA, NEPSE was higher a week later only 34% of the time (vs 48%
  normally).
- **Breadth is a risk filter, not a return booster:** after costs no rule clearly
  beat buy-and-hold, but e.g. holding only while the summation index rises
  kept a similar return with about half the drawdown (−21% vs −42%).
- **Textbook divergences fail here:** NEPSE highs without AD-line confirmation
  were followed by *above*-average returns. Breadth thrusts and washouts weren't
  reliable turning points either. NEPSE behaves like a momentum market.
- **Mutual funds don't change the signals** (< 0.01 difference); excluding them
  is cleaner, not more predictive.
- **Limits:** the best reading explains only ~1–4% of next-week variation. The big
  cycles are liquidity-driven (interest rates, NRB margin rules), which isn't in
  this data; a liquidity series is the natural next thing to add.

Mutual funds trade near their NPR 10 face value and debentures near NPR
1,000, so `scripts/securities.py` classifies them automatically (newly listed
funds are picked up without editing a list); the result is written to
`data/security-types.csv`.

The site is rebuilt and deployed by `.github/workflows/update-breadth-chart.yml`
every time the daily price scraper finishes. The scraper's first run is at
15:15 Nepal time, right after NEPSE's 15:00 close, so the chart is current
shortly after each trading day ends. The page source lives in `site/`;
`python scripts/build_site.py` builds it into `_site/` (open it with
`python -m http.server -d _site`).

**One-time setup:** in the repository's *Settings → Pages*, set **Source** to
**GitHub Actions**. After that, deployments are automatic.

## Market breadth (advancers/decliners, AD Line, NEPSE index)

This fork adds a market-breadth pipeline on top of the company-wise data above.
It runs via `.github/workflows/update-breadth-chart.yml` each time the main
data-collection workflow finishes, and produces:

- `data/market-breadth.csv` — one row per trading day: `advancers`, `decliners`,
  `unchanged`, `total_traded`, counted from every company-wise CSV.
- `data/nepse-index.csv` — the NEPSE index's own daily `open/high/low/close`,
  pulled separately from ShareSansar's index history (index_id 12), back to
  1997-07-20. This is the overall market index, not any single company's price.
- `data/combined.csv` — the two merged, plus `net_advances` (advancers −
  decliners), `ad_line` (cumulative net_advances since 1995 — the classic
  Advance/Decline Line), and 5-day/7-day moving averages of both the AD line
  and the NEPSE close.
- `charts/ad_line_recent.png` — last 250 trading days (AD line rebased to 0 at
  the window start, so short-term moves are visible against a ~30-year-old
  cumulative total).
- `charts/ad_line_full.png` — the full history.

![NEPSE market breadth, recent](charts/ad_line_recent.png)

Scripts live in `scripts/` and run in order: `fetch_nepse_index.py` →
`build_breadth.py` → `build_combined.py` → `plot_chart.py`.

## Contributing

Pull requests are welcome. Please open an issue first to discuss what you would like to change.

Things you can consider doing:

- Github action to update data on a daily basis on Kaggle
- Date-wise Collection of data from past to present

## License

[MIT](https://choosealicense.com/licenses/mit/)
