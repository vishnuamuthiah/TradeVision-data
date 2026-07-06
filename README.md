# TradeVision data

Public data pipeline for the TradeVision app. Two independent weekly GitHub Actions
jobs scrape market data and publish compact per-ticker JSON as **Release assets**,
which the app downloads on device. They run in parallel and fail in isolation — a
dividend outage never blocks earnings from publishing.

| Feed | Source | Script | Workflow | Stable URL |
|------|--------|--------|----------|------------|
| Earnings | Nasdaq earnings calendar (all exchanges) | [`build_earnings.py`](scraper/build_earnings.py) | [`earnings.yml`](.github/workflows/earnings.yml) | `releases/download/earnings-latest/earnings.json` |
| Dividends | Yahoo, per symbol (SEC ticker universe) | [`build_dividends.py`](scraper/build_dividends.py) | [`dividends.yml`](.github/workflows/dividends.yml) | `releases/download/dividends-latest/dividends.json` |

Dividends come from Yahoo, not Nasdaq, because Nasdaq's dividend API silently drops
many major NYSE payers (KO, JNJ, PG, XOM…). Both jobs run Mondays 08:00 UTC plus
manual `workflow_dispatch`, and fail loudly (canary checks / sanity floors) if a
source is blocked, so the owner is notified.

This repo holds no app source — only the scrapers and the public data they produce.
