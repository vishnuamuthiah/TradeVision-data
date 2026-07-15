# TradeVision data

Public data pipeline for the TradeVision app. Weekly GitHub Actions jobs scrape market
data and publish compact per-ticker JSON as **Release assets**, which the app downloads
on device. The earnings and dividend jobs run in parallel and fail in isolation — a
dividend outage never blocks earnings from publishing.

| Feed | Source | Script | Workflow | Stable URL |
|------|--------|--------|----------|------------|
| Tickers | Cboe option-listed symbol directory (~5.3k optionable names) | [`build_tickers.py`](scraper/build_tickers.py) | [`tickers.yml`](.github/workflows/tickers.yml) | `releases/download/tickers-latest/tickers.json` |
| Earnings | Nasdaq earnings calendar, filtered to the optionable universe | [`build_earnings.py`](scraper/build_earnings.py) | [`earnings.yml`](.github/workflows/earnings.yml) | `releases/download/earnings-latest/earnings.json` |
| Dividends | Yahoo, per optionable symbol | [`build_dividends.py`](scraper/build_dividends.py) | [`dividends.yml`](.github/workflows/dividends.yml) | `releases/download/dividends-latest/dividends.json` |
| IV Rank | Cboe `iv30` weekly, seeded once from MarketData.app history | [`build_iv_weekly.py`](scraper/build_iv_weekly.py) | [`iv.yml`](.github/workflows/iv.yml) | `releases/download/iv-latest/iv.json` |

The **Tickers** job runs first each week (Mondays 08:00 UTC), refreshes the optionable
universe from Cboe, and on completion triggers the earnings and dividend jobs via
`workflow_run`. Both consume `tickers.json` so all three feeds share one universe;
TradeVision only prices options strategies, so non-optionable names are never looked
up. If a tickers run hiccups, the downstream jobs fall back to the last-published
tickers asset (dividends then to a direct Cboe fetch), so they degrade gracefully.

Dividends come from Yahoo, not Nasdaq, because Nasdaq's dividend API silently drops
many major NYSE payers (KO, JNJ, PG, XOM…). All jobs also expose manual
`workflow_dispatch`, and fail loudly (canary checks / sanity floors) if a source is
blocked, so the owner is notified.

The **IV Rank** feed is different: it *accumulates*. IV Rank / percentile need a
trailing 52-week window of 30-day constant-maturity ATM implied vol, and no free source
sells that history. So [`build_iv_weekly.py`](scraper/build_iv_weekly.py) samples Cboe's
`iv30` for a fixed 400-name list ([`data/iv_symbols.csv`](data/iv_symbols.csv)) every
Saturday, appends it to a committed `data/history.csv`, recomputes the rolling window,
and publishes `iv.json` (per symbol: `iv30, iv52High, iv52Low, ivRank, ivPercentile`).
The one-time [`build_iv_backfill.py`](scraper/build_iv_backfill.py) (manual
[`iv_backfill.yml`](.github/workflows/iv_backfill.yml), needs an `MD_TOKEN` secret) seeds
that history from MarketData.app so IV Rank works at launch — it solves Black–Scholes IV
on ATM historical mid prices, which matches Cboe's live `iv30` within ~0.1 vol pt. Seed
points age out within a year, after which the window is 100% Cboe-sampled.

This repo holds no app source — only the scrapers and the public data they produce.
