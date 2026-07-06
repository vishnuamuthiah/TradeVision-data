"""Shared helpers for the TradeVision data scrapers (earnings + dividends)."""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import timedelta

# Nasdaq wants a full browser UA; Yahoo's rate-limiter counterintuitively 429s the
# full Chrome UA but lets a bare "Mozilla/5.0" through.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36")
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


def write_output(path, obj, label):
    """Write compact JSON and log size."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    mb = os.path.getsize(path) / 1_048_576
    print(f"Wrote {path}: {label} ({mb:.2f} MB)", file=sys.stderr)
