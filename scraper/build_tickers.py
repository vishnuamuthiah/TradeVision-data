#!/usr/bin/env python3
"""
Tickers half of the TradeVision feed: Cboe optionable universe -> tickers.json.

The canonical option-listed symbol directory (~5.3k names), refreshed weekly and
published as its own Release asset. It runs BEFORE the earnings and dividend builds
(which trigger off this workflow's completion) and both consume it, so all three feeds
share one universe. TradeVision only prices options strategies, so the only underlyings
it can ever look up are optionable ones.

Schema:

    {
      "generatedAt": "2026-07-06T08:00:00Z",
      "source": "cboe",
      "count": 5303,
      "symbols": ["A", "AA", "AAAU", ...]   # sorted, deduped, uppercase; class shares "."
    }
"""
import os
import sys
from datetime import datetime, timezone

from common import cboe_universe, write_output

OUT_PATH = os.environ.get("OUT_PATH", "data/tickers.json")


def build():
    syms = cboe_universe()
    if len(syms) < 3000:
        sys.exit(f"ERROR: Cboe universe only {len(syms)} symbols — fetch failed.")
    print(f"Universe: {len(syms)} symbols (Cboe optionable)", file=sys.stderr)

    out = {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "cboe",
        "count": len(syms),
        "symbols": syms,
    }
    write_output(OUT_PATH, out, f"{len(syms)} optionable symbols")


if __name__ == "__main__":
    build()
