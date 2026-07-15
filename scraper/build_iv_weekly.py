#!/usr/bin/env python3
"""
Weekly IV maintenance: append this week's Cboe iv30 to history.csv, recompute the
rolling 52-week window, and publish the client asset iv.json.

Runs free every week (no MarketData token — Cboe's delayed-quotes endpoint gives a
clean 30-day constant-maturity ATM iv30 directly). The one-time MarketData backfill
(`build_iv_backfill.py`) seeds history.csv so IV Rank works on day one; from then on
this job carries it, and within ~52 weeks every hand-seeded point ages out and the
window is 100% Cboe-sampled and self-consistent.

  data/history.csv   accumulating truth: symbol,date,iv30,source  (committed each run)
  data/iv.json       derived client asset, published as Release asset iv-latest:
    { "generatedAt","tradeDate","count",
      "symbols": { "AAPL": {"iv30","iv52High","iv52Low","ivRank","ivPercentile",
                            "samples","asOf"}, ... } }

iv30 is in percent points. ivRank/ivPercentile are 0-100 ints, or null when a symbol
has too little history for the number to mean anything.
"""
import csv, gzip, io, json, os, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import urllib.request, urllib.error

from common import write_output, BROWSER_UA

HISTORY = os.environ.get("HISTORY_PATH", "data/history.csv")
OUT     = os.environ.get("OUT_PATH", "data/iv.json")
SYMBOLS = os.environ.get("SYMBOLS", "data/iv_symbols.csv")
WORKERS = int(os.environ.get("WORKERS", "4"))
WINDOW_DAYS = 365
MIN_SAMPLES = 8            # below this, IV Rank/percentile are null (not enough history)
CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"


def cboe_iv30(sym, retries=6):
    """(iv30_pct, trade_date) from Cboe's delayed-quotes summary, or None.
    Index roots (SPX/VIX/...) 403 under their plain name -> retry with a '_' prefix.
    Cboe's CDN rate-limits bursts with 429s, so back off generously on those."""
    variants = [sym] + (["_" + sym] if "." not in sym else [])
    headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
    for variant in variants:
        for attempt in range(retries):
            try:
                req = urllib.request.Request(CHAIN_URL.format(variant), headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                data = (json.loads(raw) or {}).get("data") or {}
                iv = data.get("iv30")
                if iv is None: return None
                td = (data.get("last_trade_time") or "")[:10] or None
                return round(float(iv), 2), td
            except urllib.error.HTTPError as e:
                if e.code == 403: break               # wrong name form; try next variant
                if e.code in (429, 502, 503): time.sleep(2 + 2 * attempt); continue
                return None
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                time.sleep(1.0 * (attempt + 1))
    return None


def load_symbols():
    with open(SYMBOLS) as f:
        return [r[0] for r in csv.reader(f)][1:]


def load_history():
    rows = []
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            for r in csv.DictReader(f):
                rows.append((r["symbol"], r["date"], float(r["iv30"]), r.get("source", "")))
    return rows


def fetch_current(syms):
    """This week's Cboe iv30 per symbol + the canonical trade date (the modal one).
    A low-concurrency first pass, then a sequential mop-up of anything the CDN
    rate-limited, so we reliably land ~all 400 without hammering Cboe."""
    got, dates = {}, Counter()

    def record(s, res):
        if res is None: return
        iv, td = res
        got[s] = iv
        if td: dates[td] += 1

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(cboe_iv30, s): s for s in syms}
        for fut in as_completed(futs):
            record(futs[fut], fut.result())

    missing = [s for s in syms if s not in got]
    if missing:
        print(f"  mop-up: retrying {len(missing)} rate-limited symbols sequentially...",
              file=sys.stderr)
        for s in missing:
            record(s, cboe_iv30(s))
            time.sleep(0.4)

    trade_date = dates.most_common(1)[0][0] if dates else date.today().isoformat()
    return got, trade_date


def summarize(series, current):
    """Rolling 52-week IV Rank / percentile from a symbol's (date,iv30) samples."""
    cutoff = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    window = [iv for d, iv in series if d >= cutoff]
    if not window: return None
    hi, lo = max(window), min(window)
    rank = pct = None
    if len(window) >= MIN_SAMPLES and hi > lo:
        rank = round((current - lo) / (hi - lo) * 100)
        rank = max(0, min(100, rank))
        pct = round(100 * sum(1 for v in window if v < current) / len(window))
    return {"iv30": round(current, 2), "iv52High": round(hi, 2), "iv52Low": round(lo, 2),
            "ivRank": rank, "ivPercentile": pct, "samples": len(window)}


def main():
    syms = load_symbols()
    rows = load_history()
    print(f"loaded {len(rows)} history rows; fetching Cboe iv30 for {len(syms)} symbols "
          f"({WORKERS} workers)...", file=sys.stderr)

    current, trade_date = fetch_current(syms)
    print(f"got {len(current)} iv30 values; trade date {trade_date}", file=sys.stderr)

    # Append this week's samples, idempotently (skip any (symbol, trade_date) already present).
    existing = {(s, d) for s, d, _, _ in rows}
    added = 0
    for s, iv in current.items():
        if (s, trade_date) not in existing:
            rows.append((s, trade_date, iv, "cboe")); added += 1
    rows.sort()

    with open(HISTORY, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "date", "iv30", "source"]); w.writerows(rows)
    print(f"history.csv: +{added} rows -> {len(rows)} total", file=sys.stderr)

    by_symbol = defaultdict(list)
    for s, d, iv, _ in rows:
        by_symbol[s].append((d, iv))
    out_syms = {}
    for s in syms:
        series = sorted(by_symbol.get(s, []))
        cur = current.get(s, series[-1][1] if series else None)
        if cur is None: continue
        summ = summarize(series, cur)
        if summ:
            summ["asOf"] = trade_date
            out_syms[s] = summ

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"),
        "tradeDate": trade_date,
        "count": len(out_syms),
        "symbols": out_syms,
    }
    ranked = sum(1 for v in out_syms.values() if v["ivRank"] is not None)
    write_output(OUT, out, f"{len(out_syms)} symbols ({ranked} with IV Rank)")


if __name__ == "__main__":
    main()
