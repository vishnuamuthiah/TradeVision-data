#!/usr/bin/env python3
"""
Earnings half of the TradeVision feed: Nasdaq's earnings calendar -> earnings.json.

Runs independently of the dividend build (separate workflow, separate Release asset)
so the two can run in parallel and fail in isolation — a Yahoo dividend outage never
blocks earnings from publishing.

Schema (keyed by symbol; app ships last-4 + next and projects further out with "~"):

    {
      "generatedAt": "2026-07-06T08:00:00Z",
      "source": "nasdaq",
      "window": {"back": 400, "forward": 100},
      "tickers": {
        "AAPL": {"past": ["2025-08-01", ...≤4], "next": "2026-07-31", "nextConfirmed": true}
      }
    }
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from common import fetch_json, trading_days, write_output, load_published_tickers, NASDAQ_HEADERS

DAYS_BACK = int(os.environ.get("DAYS_BACK", "400"))         # ~4 quarters of history
DAYS_FORWARD = int(os.environ.get("DAYS_FORWARD", "100"))   # announced upcoming reports
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.8"))
OUT_PATH = os.environ.get("OUT_PATH", "data/earnings.json")


def scrape(start, end):
    """{symbol: set(iso report dates)} — the report date is the queried day."""
    by_symbol = {}
    for d in trading_days(start, end):
        iso = d.isoformat()
        data = fetch_json(f"https://api.nasdaq.com/api/calendar/earnings?date={iso}", NASDAQ_HEADERS)
        rows = ((data or {}).get("data") or {}).get("rows") or []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                by_symbol.setdefault(sym, set()).add(iso)
        print(f"earnings {iso}: {len(rows)} rows", file=sys.stderr)
        time.sleep(REQUEST_DELAY)
    return by_symbol


def build():
    today = date.today()
    start, end = today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_FORWARD)
    today_iso = today.isoformat()

    print(f"Earnings scan {start} .. {end}", file=sys.stderr)
    earnings = scrape(start, end)

    # Restrict to the optionable universe published weekly by the tickers feed — the
    # app only prices optionable underlyings, so non-optionable names are unusable. If
    # the asset is unavailable, emit unfiltered rather than blocking the publish.
    allow = set(load_published_tickers())
    if allow:
        print(f"Filtering to {len(allow)} optionable symbols", file=sys.stderr)
    else:
        print("WARNING: optionable list unavailable — emitting unfiltered earnings", file=sys.stderr)

    tickers = {}
    for sym, dates in earnings.items():
        if allow and sym not in allow:
            continue
        ds = sorted(dates)
        past = [d for d in ds if d <= today_iso][-4:]
        future = [d for d in ds if d > today_iso]
        if past or future:
            tickers[sym] = {"past": past, "next": future[0] if future else None,
                            "nextConfirmed": bool(future)}

    if len(tickers) < 500:
        sys.exit(f"ERROR: only {len(tickers)} earnings tickers — Nasdaq likely blocked.")

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "nasdaq",
        "window": {"back": DAYS_BACK, "forward": DAYS_FORWARD},
        "tickers": tickers,
    }
    write_output(OUT_PATH, out, f"{len(tickers)} tickers")


if __name__ == "__main__":
    build()
