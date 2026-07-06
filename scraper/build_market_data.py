#!/usr/bin/env python3
"""
Build TradeVision's on-device market-data file: per-ticker earnings dates (Nasdaq)
and the latest dividend (Yahoo).

Hybrid sourcing, on purpose:
  * Earnings  — Nasdaq's calendar endpoint, scanned day-by-day. Covers every
                exchange, and one scan returns all reporting symbols (few requests).
  * Dividends — Yahoo's chart endpoint, per symbol. Nasdaq's dividend API only
                covers Nasdaq-LISTED names (NYSE blue-chips like KO/JNJ/XOM come back
                empty), so it can't be the dividend source. Yahoo covers both.

Output schema (keyed by symbol so the app does an O(1) lookup):

    {
      "generatedAt": "2026-07-06T08:00:00Z",
      "source": "nasdaq-earnings+yahoo-dividends",
      "window": {"back": 400, "forward": 100},
      "tickers": {
        "AAPL": {
          "earnings": {
            "past": ["2025-08-01","2025-10-30","2026-01-29","2026-05-01"],
            "next": "2026-07-31",          # from the forward calendar, or null
            "nextConfirmed": true          # true = on Nasdaq's calendar (not a guess)
          },
          "dividend": {                    # absent if the name pays no dividend
            "exDate": "2026-05-11",
            "amount": 0.27,
            "indicatedAnnual": 1.08,
            "paymentDate": null,           # Yahoo chart doesn't carry it; app uses ex-date
            "frequency": 4                 # 1/2/4/12, or null for a special/irregular
          }
        }
      }
    }

The app stores last-4 + next; it PROJECTS further-out earnings and dividends itself
(from the cadence) with a leading "~", so we only ship confirmed points here.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

DAYS_BACK = int(os.environ.get("DAYS_BACK", "400"))        # ~4 quarters of earnings history
DAYS_FORWARD = int(os.environ.get("DAYS_FORWARD", "100"))  # announced upcoming earnings
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.8"))   # Nasdaq calendar politeness
YAHOO_DELAY = float(os.environ.get("YAHOO_DELAY", "0.2"))       # per-symbol Yahoo politeness
OUT_PATH = os.environ.get("OUT_PATH", "data/market_data.json")

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36")
# Yahoo's rate-limiter 429s the full Chrome UA but lets a bare "Mozilla/5.0" through.
YAHOO_UA = "Mozilla/5.0"
NASDAQ_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_json(url, headers, retries=5, backoff_codes=(429, 502, 503, 999)):
    """GET JSON with polite backoff. Returns parsed JSON, or None after retries.
    Retries transient/rate-limit statuses; gives up immediately on hard 4xx."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in backoff_codes:
                return None
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(2 ** attempt)
    return None


def trading_days(start, end):
    """Weekdays in [start, end] inclusive (Mon–Fri)."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ------------------------------- earnings (Nasdaq) -------------------------------

def scrape_earnings(start, end):
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


# ------------------------------- dividends (Yahoo) -------------------------------

def yahoo_symbol(sym):
    """Yahoo uses '-' for class shares where Nasdaq uses '.'/'/' (BRK.B -> BRK-B)."""
    return sym.replace(".", "-").replace("/", "-")


def fetch_dividend(symbol):
    """Latest dividend for `symbol` from Yahoo's chart events, or None if the name
    pays no dividend / Yahoo has nothing. Returns the app's dividend dict."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(symbol)}"
           "?range=2y&interval=1d&events=div")
    data = fetch_json(url, {"User-Agent": YAHOO_UA})
    result = ((data or {}).get("chart") or {}).get("result") or []
    if not result:
        return None
    events = (result[0].get("events") or {}).get("dividends") or {}
    divs = sorted((datetime.fromtimestamp(int(v["date"]), timezone.utc).date(), float(v["amount"]))
                  for v in events.values() if v.get("amount"))
    if not divs:
        return None

    ex, amount = divs[-1]
    # Frequency from the median gap between recent ex-dates — robust to trailing-window
    # edge effects (a 12-month window catches 12 OR 13 monthly payments) and to a
    # single special dividend. Map the gap to a clean cadence; the app projects the
    # next ex-date from it. Only a lone dividend leaves frequency unknown (null).
    recent = divs[-6:]
    gaps = sorted((recent[i][0] - recent[i - 1][0]).days for i in range(1, len(recent)))
    freq = None
    if gaps:
        g = gaps[len(gaps) // 2]
        freq = 12 if g <= 45 else 4 if g <= 135 else 2 if g <= 270 else 1
    annual = round(amount * freq, 4) if freq else round(sum(a for _, a in divs[-4:]), 4)
    return {
        "exDate": ex.isoformat(),
        "amount": amount,
        "indicatedAnnual": annual,
        "paymentDate": None,
        "frequency": freq,
    }


def scrape_dividends(symbols):
    """{symbol: dividend dict} for every dividend-paying name in `symbols`."""
    out = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        entry = fetch_dividend(sym)
        if entry:
            out[sym] = entry
        if i % 250 == 0 or i == total:
            print(f"dividends {i}/{total} scanned, {len(out)} paying", file=sys.stderr)
        time.sleep(YAHOO_DELAY)
    return out


# ------------------------------------ build -------------------------------------

def build():
    today = date.today()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_FORWARD)
    today_iso = today.isoformat()

    print(f"Earnings scan {start} .. {end}", file=sys.stderr)
    earnings = scrape_earnings(start, end)

    universe = sorted(earnings)
    print(f"Dividend scan over {len(universe)} earnings-universe symbols", file=sys.stderr)
    dividends = scrape_dividends(universe)

    tickers = {}
    for sym in set(earnings) | set(dividends):
        dates = sorted(earnings.get(sym, set()))
        past = [d for d in dates if d <= today_iso][-4:]
        future = [d for d in dates if d > today_iso]
        entry = {}
        if past or future:
            entry["earnings"] = {"past": past, "next": future[0] if future else None,
                                 "nextConfirmed": bool(future)}
        if sym in dividends:
            entry["dividend"] = dividends[sym]
        if entry:
            tickers[sym] = entry

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "nasdaq-earnings+yahoo-dividends",
        "window": {"back": DAYS_BACK, "forward": DAYS_FORWARD},
        "tickers": tickers,
    }

    # Sanity floors — fail loudly (workflow emails owner) rather than ship bad data:
    #   * a whole-market earnings scan yields thousands of names;
    #   * Yahoo canaries (major NYSE payers) prove dividends weren't IP-blocked.
    if len(tickers) < 500:
        sys.exit(f"ERROR: only {len(tickers)} tickers — Nasdaq earnings likely blocked.")
    canaries = [c for c in ("KO", "JNJ", "PG") if c in universe]
    missing = [c for c in canaries if c not in dividends]
    if canaries and missing:
        sys.exit(f"ERROR: dividend canaries missing {missing} — Yahoo likely blocked/rate-limited.")

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size_mb = os.path.getsize(OUT_PATH) / 1_048_576
    print(f"Wrote {OUT_PATH}: {len(tickers)} tickers, {len(dividends)} with dividends, "
          f"{size_mb:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    build()
