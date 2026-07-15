#!/usr/bin/env python3
"""
One-time IV history backfill from MarketData.app historical EOD chains -> history.csv.

This SEEDS the 52-week IV history that IV Rank needs on day one. After this runs once,
`build_iv_weekly.py` maintains the series for free from Cboe. Run it manually (locally
or via the `workflow_dispatch` on iv.yml) with a MarketData token in MD_TOKEN.

Why we solve IV ourselves: MarketData returns historical option *prices* but not
IV/greeks, so we Black-Scholes the ATM mid price. Validated to match Cboe's live iv30
within ~0.1 vol pt (averaging the ATM call & put IV cancels most of the rate/dividend
error via put-call parity). For each (symbol, weekly Friday) we pull the ATM strikes of
the expirations bracketing 30 DTE in ONE call (1 credit) and interpolate in total
variance to a constant 30-day maturity.

Cost: ~1 credit/call, 400 symbols x 52 weeks ~= 20.8k credits (one day of a Trader plan).
Output rows: symbol,date,iv30,source   (source="md-bs", iv30 in percent points).
"""
import csv, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import urllib.request, urllib.error, json

TOKEN   = os.environ["MD_TOKEN"]
OUT     = os.environ.get("OUT_PATH", "data/history.csv")
SYMBOLS = os.environ.get("SYMBOLS", "data/iv_symbols.csv")
WEEKS   = int(os.environ.get("WEEKS", "52"))
WORKERS = int(os.environ.get("WORKERS", "8"))
LIMIT   = int(os.environ.get("LIMIT", "0"))         # >0 = only first N symbols (testing)
R = 0.045                                           # flat risk-free; ATM call/put avg cancels most of it


def norm_cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(cp, S, K, T, r, sig):
    if sig <= 0 or T <= 0:
        return max(0.0, (S - K) if cp == "call" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if cp == "call":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def implied_vol(cp, S, K, T, r, price):
    intrinsic = max(0.0, (S - K) if cp == "call" else (K - S))
    if price <= intrinsic + 1e-6 or T <= 0:         # no time value -> unsolvable
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(60):                             # bisection: robust, ~1e-15 precision
        mid = (lo + hi) / 2
        if bs(cp, S, K, T, r, mid) > price: hi = mid
        else: lo = mid
    return (lo + hi) / 2


def atm_iv(contracts, S):
    """ATM implied vol for one expiration: nearest strike to spot, avg of call & put."""
    T = contracts[0]["dte"] / 365.0
    by_strike = {}
    for c in contracts:
        v = implied_vol(c["side"], S, c["strike"], T, R, c["mid"])
        if v: by_strike.setdefault(c["strike"], []).append(v)
    if not by_strike: return None
    k = min(by_strike, key=lambda x: abs(x - S))    # strike nearest spot
    vs = by_strike[k]
    return contracts[0]["dte"], sum(vs) / len(vs)


def fetch(sym, d, retries=4):
    """MarketData ATM chain for the expirations bracketing 30 DTE, as-of date d."""
    frm = (d + timedelta(days=16)).isoformat()
    to  = (d + timedelta(days=45)).isoformat()
    url = (f"https://api.marketdata.app/v1/options/chain/{sym}/"
           f"?date={d.isoformat()}&from={frm}&to={to}&strikeLimit=2")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read())
            if j.get("s") not in ("ok", None): return None
            n = len(j.get("optionSymbol", []))
            return [{k: j[k][i] for k in ("side", "strike", "dte", "mid", "underlyingPrice")}
                    for i in range(n) if j.get("mid", [None])[i]]
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(1.5 * (attempt + 1)); continue
            return None
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1.0 * (attempt + 1))
    return None


def _iv30_on(sym, d):
    rows = fetch(sym, d)
    if not rows: return None
    S = rows[0]["underlyingPrice"]
    by_exp = {}
    for c in rows: by_exp.setdefault(c["dte"], []).append(c)
    pts = [p for p in (atm_iv(cs, S) for cs in by_exp.values()) if p]   # (dte, iv)
    if not pts: return None
    pts.sort()
    below = [p for p in pts if p[0] <= 30]
    above = [p for p in pts if p[0] >= 30]
    if below and above:                             # constant-maturity 30d via total-variance interp
        (t1, v1), (t2, v2) = below[-1], above[0]
        if t1 == t2: iv = v1
        else:
            w1, w2 = v1*v1*(t1/365), v2*v2*(t2/365)
            w = w1 + (w2 - w1) * (30 - t1) / (t2 - t1)
            iv = math.sqrt(w / (30/365))
    else:                                           # 30 outside available range -> nearest expiration
        iv = min(pts, key=lambda p: abs(p[0] - 30))[1]
    return round(iv * 100, 2)


def iv30_for(sym, d):
    # A target Friday may be a market holiday (Juneteenth, July 4th, Good Friday...);
    # step back up to 3 days to the prior trading day so the weekly series has no gaps.
    for back in range(4):
        iv = _iv30_on(sym, d - timedelta(days=back))
        if iv is not None: return iv
    return None


def weekly_fridays(weeks):
    d = date.today()
    d -= timedelta(days=(d.weekday() - 4) % 7 or 7)  # most recent past Friday
    return [d - timedelta(weeks=i) for i in range(weeks)][::-1]


def load_symbols():
    with open(SYMBOLS) as f:
        return [r[0] for r in csv.reader(f)][1:]


def main():
    syms = load_symbols()
    if LIMIT: syms = syms[:LIMIT]
    dates = weekly_fridays(WEEKS)
    jobs = [(s, d) for s in syms for d in dates]
    print(f"{len(syms)} symbols x {len(dates)} weeks = {len(jobs)} calls "
          f"({WORKERS} workers)", file=sys.stderr)
    out, done, fails = [], 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(iv30_for, s, d): (s, d) for s, d in jobs}
        for fut in as_completed(futs):
            s, d = futs[fut]; done += 1
            if done % 500 == 0: print(f"  ...{done}/{len(jobs)}", file=sys.stderr)
            try: iv = fut.result()
            except Exception: iv = None
            if iv is None: fails += 1; continue
            out.append((s, d.isoformat(), iv, "md-bs"))
    out.sort()
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "date", "iv30", "source"]); w.writerows(out)
    print(f"wrote {OUT}: {len(out)} rows ({fails} failed of {len(jobs)})", file=sys.stderr)


if __name__ == "__main__":
    main()
