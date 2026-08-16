"""Read-only: run the real ingest-time term classifiers against live contracts.

Reproduces exactly what `_terms_fields` would store today, so "WeatherModel
68 -> 0" gets a cause rather than a theory. GET only, no DB writes.

Run:  PYTHONPATH=. python3 tasks/diag_terms.py
"""
from __future__ import annotations

import asyncio
import sys

from src.config import Settings
from src.ingestion.market_sync import _terms_fields
from src.kalshi.client import KalshiClient
from src.weather.stations import STATIONS
from src.weather.terms import (
    is_in_scope,
    is_temperature_market,
    is_unsupported_type,
    parse_contract_terms,
)


async def main() -> int:
    settings = Settings()
    if settings.is_offline_mode:
        print("NO CREDENTIALS — refusing to report anything")
        return 1
    client = KalshiClient.from_settings(settings)
    totals = {}
    try:
        for ticker in STATIONS:
            markets = await client.get_series_markets(ticker, max_markets=40)
            print(f"\n===== {ticker}: {len(markets)} markets =====")
            for m in markets[:3]:
                print(f"  ticker={m.ticker}")
                print(f"    title           : {m.title!r}")
                print(f"    subtitle        : {m.subtitle!r} / yes_sub_title={m.yes_sub_title!r}")
                print(f"    strike_type     : {m.strike_type!r} floor={m.floor_strike} cap={m.cap_strike}")
                print(f"    is_temperature  : {is_temperature_market(m)}")
                print(f"    is_in_scope     : {is_in_scope(m)}")
                print(f"    unsupported_type: {is_unsupported_type(m)}")
                print(f"    parse_terms     : {parse_contract_terms(m)}")
                print(f"    STORED          : {_terms_fields(m)['terms_status']}")
            for m in markets:
                key = (ticker, _terms_fields(m)["terms_status"])
                totals[key] = totals.get(key, 0) + 1
    finally:
        await client._http.aclose()

    print("\n===== terms_status totals per series =====")
    for (series, status), n in sorted(totals.items()):
        print(f"  {series:12s} {status:20s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
