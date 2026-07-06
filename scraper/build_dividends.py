#!/usr/bin/env python3
"""
Dividend half of the TradeVision feed: Yahoo per-symbol -> dividends.json.

Yahoo (not Nasdaq) because Nasdaq's dividend API silently drops many major NYSE
payers (KO, JNJ, PG, XOM...). This build is fully independent of the earnings build
(they run in parallel).

Universe = the optionable list published weekly by the tickers feed (Cboe's ~5.3k
option-listed names), NOT the full ~10.4k SEC ticker list. TradeVision only prices
options strategies, so the only underlyings it can ever look up are optionable ones;
the SEC universe pushed past Yahoo's ~7,500 throttle (Actions IP stalls near 7,500,
then 429s the S-Z tail into the timeout). The Cboe list is under that throttle point.
Falls back to a direct Cboe fetch if the published tickers asset is unavailable.

Schema (keyed by symbol; app projects further-out ex-dates from `frequency` with "~"):

    {
      "generatedAt": "2026-07-06T08:00:00Z",
      "source": "yahoo",
      "tickers": {
        "KO": {"exDate": "2026-06-15", "amount": 0.53, "indicatedAnnual": 2.12,
               "paymentDate": null, "frequency": 4}   # frequency null = special/irregular
      }
    }
"""
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

from common import fetch_json, write_output, cboe_universe, load_published_tickers, YAHOO_UA

# ~0.4s + jitter keeps us comfortably under Yahoo's rate limiter across the ~5.3k
# optionable universe; jitter avoids a perfectly periodic request pattern.
YAHOO_DELAY = float(os.environ.get("YAHOO_DELAY", "0.4"))
YAHOO_JITTER = float(os.environ.get("YAHOO_JITTER", "0.2"))
OUT_PATH = os.environ.get("OUT_PATH", "data/dividends.json")


def yahoo_symbol(sym):
    """Yahoo uses '-' for class shares where SEC uses '.'/'/' (BRK.B -> BRK-B)."""
    return sym.replace(".", "-").replace("/", "-")


def fetch_dividend(symbol):
    """Latest dividend for `symbol` from Yahoo chart events, or None if it pays none."""
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
    # edge effects and a single special dividend. Only a lone dividend leaves it null.
    recent = divs[-6:]
    gaps = sorted((recent[i][0] - recent[i - 1][0]).days for i in range(1, len(recent)))
    freq = None
    if gaps:
        g = gaps[len(gaps) // 2]
        freq = 12 if g <= 45 else 4 if g <= 135 else 2 if g <= 270 else 1
    annual = round(amount * freq, 4) if freq else round(sum(a for _, a in divs[-4:]), 4)
    return {"exDate": ex.isoformat(), "amount": amount, "indicatedAnnual": annual,
            "paymentDate": None, "frequency": freq}


def build():
    syms = load_published_tickers() or cboe_universe()
    if len(syms) < 3000:
        sys.exit(f"ERROR: optionable universe only {len(syms)} symbols — fetch failed.")
    print(f"Universe: {len(syms)} symbols (optionable)", file=sys.stderr)

    dividends = {}
    for i, sym in enumerate(syms, 1):
        entry = fetch_dividend(sym)
        if entry:
            dividends[sym] = entry
        if i % 250 == 0 or i == len(syms):
            print(f"dividends {i}/{len(syms)} scanned, {len(dividends)} paying", file=sys.stderr)
        time.sleep(YAHOO_DELAY + random.uniform(0, YAHOO_JITTER))

    # Fail loudly (workflow emails owner) if Yahoo blocked us: canary NYSE payers must
    # resolve, and total coverage must clear a floor.
    canaries = [c for c in ("KO", "JNJ", "PG") if c in syms]
    missing = [c for c in canaries if c not in dividends]
    if missing:
        sys.exit(f"ERROR: dividend canaries missing {missing} — Yahoo likely blocked/rate-limited.")
    if len(dividends) < 800:
        sys.exit(f"ERROR: only {len(dividends)} dividend payers — Yahoo likely throttled.")

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "yahoo",
        "tickers": dividends,
    }
    write_output(OUT_PATH, out, f"{len(dividends)} payers")


if __name__ == "__main__":
    build()
