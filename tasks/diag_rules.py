"""Read-only: pull live rules text for every mapped weather series.

Answers "is it all 7 series or just NYC" without guessing. GET only.

Run:  python3 tasks/diag_rules.py
"""
from __future__ import annotations

import asyncio
import sys

from src.config import Settings
from src.kalshi.client import KalshiClient
from src.weather.stations import STATIONS


async def main() -> int:
    settings = Settings()
    if settings.is_offline_mode:
        print("NO CREDENTIALS — refusing to report anything")
        return 1
    client = KalshiClient.from_settings(settings)
    try:
        for ticker, station in STATIONS.items():
            markets = await client.get_series_markets(ticker, max_markets=3)
            print(f"\n===== {ticker}  (mapped: {station.name} / marker {station.rules_marker!r}) =====")
            if not markets:
                print("  NO OPEN MARKETS")
                continue
            for m in markets[:1]:
                rules = (m.rules_primary or "")
                low = rules.lower()
                print(f"  ticker      : {m.ticker}")
                print(f"  close_time  : {m.close_time}")
                print(f"  status      : {m.status}")
                print(f"  marker ok   : {station.rules_marker in low}")
                print(f"  'climatological report' present : {'climatological report' in low}")
                print(f"  'weather company' present       : {'weather company' in low}")
                print(f"  'national weather service' present: {'national weather service' in low}")
                print(f"  RULES: {rules}")
    finally:
        await client._http.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
