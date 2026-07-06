# TradeVision data

Public data pipeline for the TradeVision app. A weekly GitHub Actions job scrapes
Nasdaq's public earnings and dividend calendars and publishes a compact per-ticker
JSON file as a **Release asset**, which the app downloads on device.

- Scraper: [`scraper/build_market_data.py`](scraper/build_market_data.py) (Python stdlib only)
- Workflow: [`.github/workflows/weekly-market-data.yml`](.github/workflows/weekly-market-data.yml) — Mondays 08:00 UTC, plus manual `workflow_dispatch`
- Output URL (stable): `https://github.com/vishnuamuthiah/TradeVision-data/releases/download/data-latest/market_data.json`

This repo holds no app source — only the scraper and the public data it produces.
A failed run (Nasdaq unreachable / schema changed) fails loudly so the owner is
notified.
