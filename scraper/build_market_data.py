#!/usr/bin/env python3
"""
Build TradeVision's on-device market-data file: per-ticker earnings dates and the
latest dividend, scraped from Nasdaq's public calendar endpoints.

Output schema (keyed by symbol so the app does an O(1) lookup):

    {
      "generatedAt": "2026-07-06T08:00:00Z",
      "source": "nasdaq",
      "window": {"back": 400, "forward": 100},
      "tickers": {
        "AAPL": {
          "earnings": {
            "past": ["2025-08-01","2025-10-30","2026-01-29","2026-05-01"],
            "next": "2026-07-31",          # from the forward calendar, or null
            "nextConfirmed": true          # true = on Nasdaq's calendar (not a guess)
          },
          "dividend": {                    # null if the name pays no dividend
            "exDate": "2026-05-11",
            "amount": 0.27,
            "indicatedAnnual": 1.08,
            "paymentDate": "2026-05-15",
            "frequency": 4                 # 1/2/4/12, or null for a special/one-off
          }
        }
      }
    }

The app stores last-4 + next; it PROJECTS further-out earnings and dividends itself
(from the cadence) with a leading "~", so we only ship confirmed points here.

No SEC/CIK step: the calendars already return every reporting symbol, so we just
scan the window and bucket by symbol. Weekends are skipped (empty anyway); holidays
return empty and are harmless.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

DAYS_BACK = int(os.environ.get("DAYS_BACK", "400"))       # ~4 quarters of earnings + div history
DAYS_FORWARD = int(os.environ.get("DAYS_FORWARD", "100"))  # announced upcoming events
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.8"))
OUT_PATH = os.environ.get("OUT_PATH", "data/market_data.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_json(url, retries=5):
    """GET with polite backoff. Returns parsed JSON or None after exhausting retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            wait = 2 ** attempt
            print(f"  retry {attempt+1}/{retries} ({e}) in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return None


def trading_days(start, end):
    """Weekdays in [start, end] inclusive (Mon–Fri)."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def parse_nasdaq_date(s):
    """Nasdaq 'M/D/YYYY' -> ISO 'YYYY-MM-DD'. None on junk."""
    try:
        m, d, y = (int(p) for p in s.split("/"))
        return date(y, m, d).isoformat()
    except (ValueError, AttributeError):
        return None


def infer_frequency(amount, annual):
    """indicatedAnnual / amount -> 1/2/4/12, else None (special/one-off)."""
    try:
        ratio = round(annual / amount)
    except (TypeError, ZeroDivisionError):
        return None
    return ratio if ratio in (1, 2, 4, 12) else None


def scrape_earnings(start, end):
    """{symbol: set(iso report dates)} — the report date is the queried day."""
    by_symbol = {}
    for d in trading_days(start, end):
        iso = d.isoformat()
        data = fetch_json(f"https://api.nasdaq.com/api/calendar/earnings?date={iso}")
        rows = ((data or {}).get("data") or {}).get("rows") or []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                by_symbol.setdefault(sym, set()).add(iso)
        print(f"earnings {iso}: {len(rows)} rows", file=sys.stderr)
        time.sleep(REQUEST_DELAY)
    return by_symbol


def scrape_dividends(start, end):
    """{symbol: latest dividend dict} keyed by newest ex-date seen."""
    latest = {}
    for d in trading_days(start, end):
        iso = d.isoformat()
        data = fetch_json(f"https://api.nasdaq.com/api/calendar/dividends?date={iso}")
        rows = (((data or {}).get("data") or {}).get("calendar") or {}).get("rows") or []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            ex = parse_nasdaq_date(row.get("dividend_Ex_Date"))
            if not sym or not ex:
                continue
            amount = row.get("dividend_Rate")
            annual = row.get("indicated_Annual_Dividend")
            entry = {
                "exDate": ex,
                "amount": amount,
                "indicatedAnnual": annual,
                "paymentDate": parse_nasdaq_date(row.get("payment_Date")),
                "frequency": infer_frequency(amount, annual),
            }
            # keep the most recent ex-date per symbol
            if sym not in latest or ex > latest[sym]["exDate"]:
                latest[sym] = entry
        print(f"dividends {iso}: {len(rows)} rows", file=sys.stderr)
        time.sleep(REQUEST_DELAY)
    return latest


def build():
    today = date.today()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_FORWARD)
    today_iso = today.isoformat()

    print(f"Scraping {start} .. {end}", file=sys.stderr)
    earnings = scrape_earnings(start, end)
    dividends = scrape_dividends(start, end)

    tickers = {}
    symbols = set(earnings) | set(dividends)
    for sym in symbols:
        dates = sorted(earnings.get(sym, set()))
        past = [d for d in dates if d <= today_iso][-4:]
        future = [d for d in dates if d > today_iso]
        entry = {}
        if past or future:
            entry["earnings"] = {
                "past": past,
                "next": future[0] if future else None,
                "nextConfirmed": bool(future),
            }
        if sym in dividends:
            entry["dividend"] = dividends[sym]
        if entry:
            tickers[sym] = entry

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "nasdaq",
        "window": {"back": DAYS_BACK, "forward": DAYS_FORWARD},
        "tickers": tickers,
    }

    # Sanity floor: a 400-day scan of the whole market should yield thousands of
    # names. A near-empty result means Nasdaq blocked us (silent 200s) — fail loudly
    # so the workflow (and thus you) is notified instead of shipping empty data.
    if len(tickers) < 500:
        print(f"ERROR: only {len(tickers)} tickers — endpoint likely blocked/changed.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size_mb = os.path.getsize(OUT_PATH) / 1_048_576
    print(f"Wrote {OUT_PATH}: {len(tickers)} tickers, {size_mb:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    build()
