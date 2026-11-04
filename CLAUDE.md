# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A dataset repository of historical price data for ~370 companies listed on the Nepal Stock Exchange (NEPSE), plus the Python scrapers that produce it. The data — not the code — is the product. A GitHub Actions cron keeps it current by appending each trading day's prices.

## Commands

All scrapers run from the `src/` directory and import using paths relative to it (`config.*`, `constants.*`, `utils.*`), so they must be invoked with `src/` as the working directory.

```bash
cd src
pip3 install -r requirements.txt      # Python 3.11 (what CI uses)

python3 discoverCompanies.py          # refresh constants/companyIdMap.py from the live site (do this before a backfill)
python3 allDataScrapper.py            # full historical backfill; seeds ONLY companies missing a CSV (skips existing)
python3 dailyDataScrapper.py          # append today's row to each existing CSV (idempotent)
```

There are no tests, linters configured beyond `black` (listed in requirements), or build step.

## Data layout

`data/company-wise/<SYMBOL>.csv` — one file per company, sorted ascending by date. Columns:

```
published_date,open,high,low,close,per_change,traded_quantity,traded_amount,status
```

`status` is derived, not sourced: `1` = close > open, `-1` = open > close, `0` = equal (see `utils/status.py`). The filename stem (`<SYMBOL>`) is the canonical company symbol and is used as a join key in the daily scraper.

## Architecture

Two scrapers hit two different sharesansar.com surfaces (`constants/url.py`):

1. **`allDataScrapper.py`** — historical backfill. Iterates `companyIdMap` (`constants/companyIdMap.py`), which maps each symbol to sharesansar's **internal numeric company id** (NOT the symbol). For each company it **POSTs** to the DataTables-style JSON endpoint (`company-price-history`), first with `length=1` to read `recordsTotal`, then pages **50 rows at a time**. The endpoint requires a **Laravel CSRF token + session cookie** (`utils/session.py` primes a `requests.Session` by GETting a company page, scraping the `_token`, and reusing the session cookies); `utils/params.py` builds the POST payload. `utils/history.py` handles pagination + turns the JSON into a DataFrame (drops `DT_Row_Index`, reverses to ascending date order, casts numeric columns to float / `status` to int). To avoid clobbering data, it **skips any symbol that already has a CSV** — only missing companies are seeded.

    Two hard-won gotchas: (a) the endpoint moved from GET to **POST** — a GET now returns `405`; (b) a page `length >= 100` is silently rejected (returns `recordsTotal: 0`, empty data), so `PAGE_SIZE` must stay `<= 50`.

    **`discoverCompanies.py`** regenerates `constants/companyIdMap.py` (a generated file — don't hand-edit). It reads the live universe of symbols from `today-share-price`, then for each visits `/company/<symbol>` and scrapes the numeric id from the hidden `<div id="companyid">`. It merges with the existing map (never drops existing/delisted entries) and writes the file sorted by symbol.

2. **`dailyDataScrapper.py`** — daily incremental. Scrapes the `today-share-price` HTML page (BeautifulSoup + `pandas.read_html`), reads today's date from a `<span class="text-org">`, then for each existing CSV matches the company by the `Symbol` column in the page's first table: if the last row is already today's date it is **replaced** (so later runs pick up corrections; identical values rewrite identical bytes, so it stays idempotent), a page date older than the last row is ignored, otherwise one row is **appended**. Only updates files that already exist — it does not create new company files.

Adding new companies is now largely automatic: run `discoverCompanies.py` to refresh `companyIdMap`, then `allDataScrapper.py` to seed any company that lacks a CSV. After that the daily job maintains it automatically.

`config/headers.py` supplies a browser `User-Agent` + `X-Requested-With` for the history POST API; the session cookies and CSRF token are obtained dynamically at runtime by `utils/session.py` (the old hardcoded `config/cookies.py` was removed). The daily HTML scrape needs no such headers.

`modified_csv.py` is a one-off historical data-fix script (recomputes `per_change` for rows on/before 2018-02-18). It expects to run from the repo root (`./data/company-wise`), unlike the scrapers. Not part of normal operation.

## Automation

`.github/workflows/schedule-updater.yml` runs `dailyDataScrapper.py` five times per day (Sun–Fri), the first at 15:15 NPT right after NEPSE's 15:00 close, commits any changed CSVs as "Update data", and pushes to `main`. The repeated runs are intentional retries in case an earlier run misses the daily data. The repo's commit history is overwhelmingly these automated "Update data" commits.

`.github/workflows/update-breadth-chart.yml` runs on `workflow_run` after each scraper run (and on pushes touching `site/`/`scripts/`). It runs the `scripts/` breadth pipeline (`fetch_nepse_index.py` → `build_breadth.py` → `build_combined.py` → `securities.py` → `plot_chart.py`), commits the outputs, then `build_site.py` builds `_site/` and deploys it to GitHub Pages (repo Settings → Pages → Source must be "GitHub Actions").

## Interactive site

`site/` is a static page (no build tooling) using a vendored TradingView Lightweight Charts v4.2.3 (`site/vendor/`). `scripts/build_site.py` copies it to `_site/` (gitignored) and writes `_site/data/market.json` — a shared date axis, NEPSE index OHLC, and per-security-type (`equity`/`mutual_fund`/`debenture`) daily advancer/decliner/unchanged counts — plus copies the company CSVs for the per-company price view. `site/app.js` sums the selected types and recomputes the AD line client-side, so the mutual-fund/debenture toggles need no extra data. Moving averages are also computed client-side for any user-chosen period (`state.mas`, max 3 at once — the colour limit; each period keeps its colour slot while on, slot 3 is dashed to stay distinct from the aqua up-candles) and cached per period on the price/breadth objects (`maOf`). The three chart panes stay in sync because every series has a point (or whitespace) on every shared date, so logical indexes line up. `scripts/securities.py` classifies securities: debentures by symbol pattern, mutual funds by a known list plus "median close < NPR 25". Preview locally with `python scripts/build_site.py && python -m http.server -d _site`.

`market.json` also carries per-type `ut`/`dt` (turnover in advancing/declining securities, NPR lakh) for the money-flow reading. `app.js` derives the breadth indicators (`computeIndicators`: ratio-adjusted McClellan oscillator + summation index, 10-day % advancing, 10-day money flow, and a 0–5 score) and the "Breadth signals" section: score history, AD-line crossover markers and stats. The score always uses the classic 19/39 McClellan (`b.mco`); the lower-pane McClellan and the "McClellan × 0" crossover use the user's two shortest active MAs as EMA periods (`mcPair`/`mcoFor`, falling back to 19/39), so they change with the MA chips. All forward returns there are measured **from the next day's open** (`fwd()`), never close-to-close: NEPSE's opening gap absorbs nearly all of the previous day's follow-through, so close-to-close numbers overstate any edge. The definitions mirror `scripts/signal_study.py`, a standalone research script (prints a report, writes nothing, not run in CI) that backs the static "What the data says" text in `site/index.html`. Those quoted numbers are a dated snapshot (study through Nov 2026); rerun the script before editing them.

## Gotchas

- The daily scraper derives the symbol via `str(file).split(".")[2].split("/")[-1]`, which assumes the exact path shape `../data/company-wise/SYMBOL.csv`. Changing the data directory depth or path format will break symbol extraction.
- Scrapers depend on sharesansar.com's page structure and endpoint contract; site changes (table order, column names like `Diff %`/`Vol`/`Turnover`, the date `<span>` class, the `#companyid` div, the `_token` input, GET-vs-POST) are the usual failure mode.
- The history POST endpoint silently returns an empty result (`recordsTotal: 0`) for a page `length >= 100` — keep `allDataScrapper.PAGE_SIZE <= 50`. An empty response is otherwise indistinguishable from a genuinely untraded company.
- `per_change` is stored as `nan` for the first record of a series; downstream consumers should handle it.
- Both workflows push to `main`, so each declares `permissions: contents: write` — this repo's default `GITHUB_TOKEN` is read-only. Don't drop that block.
- GitHub's `schedule` trigger is best-effort: runs can be late or skipped (especially in the first days of a new repo). If a day is missed, run `update-companies-data` by hand (Actions → Run workflow); `update-breadth-chart-and-site` follows automatically.
