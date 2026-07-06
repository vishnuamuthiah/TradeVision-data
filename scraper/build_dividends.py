#!/usr/bin/env python3
"""
Dividend half of the TradeVision feed: Yahoo per-symbol -> dividends.json.

Yahoo (not Nasdaq) because Nasdaq's dividend API silently drops many major NYSE
payers (KO, JNJ, PG, XOM...). The universe comes from SEC's company_tickers.json so
this build is fully independent of the earnings build (they run in parallel) and, in
fact, more complete than "companies that reported earnings in the window."

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
import sys
import time
from datetime import date, datetime, timedelta, timezone

from common import fetch_json, write_output, YAHOO_UA

YAHOO_DELAY = float(os.environ.get("YAHOO_DELAY", "0.2"))
OUT_PATH = os.environ.get("OUT_PATH", "data/dividends.json")
# SEC enforces a descriptive User-Agent with contact info.
SEC_HEADERS = {"User-Agent": "TradeVision data pipeline vishnuamuthiah@gmail.com",
               "Accept": "application/json"}


def universe():
    """All public tickers from SEC's company_tickers.json (independent of earnings)."""
    data = fetch_json("https://www.sec.gov/files/company_tickers.json", SEC_HEADERS)
    syms = {(row.get("ticker") or "").strip().upper()
            for row in (data or {}).values()}
    syms.discard("")
    return sorted(syms)


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
    syms = universe()
    if len(syms) < 5000:
        sys.exit(f"ERROR: SEC universe only {len(syms)} symbols — fetch failed.")
    print(f"Universe: {len(syms)} symbols (SEC)", file=sys.stderr)

    dividends = {}
    for i, sym in enumerate(syms, 1):
        entry = fetch_dividend(sym)
        if entry:
            dividends[sym] = entry
        if i % 250 == 0 or i == len(syms):
            print(f"dividends {i}/{len(syms)} scanned, {len(dividends)} paying", file=sys.stderr)
        time.sleep(YAHOO_DELAY)

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
