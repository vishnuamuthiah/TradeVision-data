"""Shared helpers for the TradeVision data scrapers (tickers + earnings + dividends)."""
import csv
import io
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

# Cboe's downloadable equity+index options symbol directory — the canonical
# "optionable" list (~5.3k names). Column 1 (0-indexed) is the stock symbol.
CBOE_URL = ("https://www.cboe.com/us/options/symboldir/equity_index_options/"
            "?download=csv")
# The tickers feed publishes the optionable universe here weekly; the earnings and
# dividend builds consume it so all three feeds share one universe.
TICKERS_URL = ("https://github.com/vishnuamuthiah/TradeVision-data/releases/download/"
               "tickers-latest/tickers.json")


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


def fetch_text(url, headers, retries=5, backoff_codes=(429, 502, 503, 999)):
    """GET a text/CSV body with the same polite backoff as fetch_json.
    Returns the decoded body, or None after retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in backoff_codes:
                return None
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
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


def cboe_universe():
    """Option-listed symbols from Cboe's symbol directory (class shares use '.',
    matching SEC/`yahoo_symbol` conventions, e.g. BRK.B). Sorted, deduped, uppercase.
    Returns [] if the fetch fails."""
    text = fetch_text(CBOE_URL, {"User-Agent": BROWSER_UA})
    if not text:
        return []
    rows = csv.reader(io.StringIO(text))
    next(rows, None)  # header: Company Name, Stock Symbol, DPM Name, ...
    syms = {row[1].strip().upper() for row in rows if len(row) > 1 and row[1].strip()}
    syms.discard("")
    return sorted(syms)


def load_published_tickers():
    """The optionable universe published weekly by the tickers feed, as a list of
    symbols. Returns [] on any failure so callers can fall back gracefully."""
    data = fetch_json(TICKERS_URL, {"User-Agent": BROWSER_UA})
    syms = (data or {}).get("symbols")
    return syms if isinstance(syms, list) else []
