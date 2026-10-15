"""Fetch and cache everything the assessment scores against. Read-only.

    python -m research.weather_assessment.fetch iem      # MEX + NBE, all stations
    python -m research.weather_assessment.fetch truth    # GHCN-Daily TMAX
    python -m research.weather_assessment.fetch kalshi   # settled markets + bid/ask

Every response is cached under research/weather_assessment/cache/ (git-ignored)
and never re-requested. Nothing here imports the trading path or touches a
database. IEM answers HTTP 429 under load, so requests are paced and back off.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from src.weather.stations import STATIONS
from src.weather.truth import fetch_daily_max

ROOT = pathlib.Path(__file__).parent / "cache"
START = dt.date(2024, 7, 1)
END = dt.date(2026, 10, 13)
# Market prices: the last 12 months (one request per market; ~40k requests).
MARKET_START = dt.date(2025, 10, 1)
# The historical candle endpoint 429s at ~1-2 requests/s, so prices are
# fetched for every 4th target day (~10k markets) rather than every day.
SAMPLE_EVERY_DAYS = 4

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = "kalshi-research (ethanperez1511@gmail.com)"
# model -> (run hour UTC used, months per request)
MODELS = {"MEX": (12, 6), "NBE": (13, 3)}


def _get(url: str, timeout: int = 180) -> tuple[int, str]:
    out = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), "-A", UA, "-w", "\n%{http_code}", url],
        capture_output=True, text=True,
    ).stdout
    body, _, code = out.rpartition("\n")
    return int(code or 0), body


def _paced(url: str, pause: float, tries: int = 8) -> str:
    wait = 60.0
    for _ in range(tries):
        code, body = _get(url)
        if code == 200:
            time.sleep(pause)
            return body
        print(f"  HTTP {code}; backing off {wait:.0f}s", flush=True)
        time.sleep(wait)
        wait = min(wait * 2, 600)
    raise RuntimeError(f"gave up on {url}")


def _chunks(months: int):
    start = START
    while start <= END:
        m = start.month - 1 + months
        stop = min(dt.date(start.year + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1), END)
        yield start, stop
        start = stop + dt.timedelta(days=1)


def fetch_iem() -> None:
    for model, (hour, months) in MODELS.items():
        for st in STATIONS.values():
            for a, b in _chunks(months):
                path = ROOT / "iem" / f"{model}_{st.mos_station}_{a}_{b}.csv"
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                q = urllib.parse.urlencode({
                    "station": st.mos_station, "model": model,
                    "sts": f"{a}T00:00Z", "ets": f"{b}T23:59Z", "format": "csv",
                })
                print(f"{model} {st.mos_station} {a}..{b}", flush=True)
                body = _paced(f"{IEM}?{q}", pause=5)
                rows = list(csv.DictReader(io.StringIO(body)))
                # Keep only the run used: MEX 12Z; NBE 13Z, or 12Z after NBM moved
                # to 00/12Z cycles in mid-2026 (both issue alongside MEX 12Z).
                hours = {f"{hour:02d}"} | ({"12"} if model == "NBE" else set())
                keep = [r for r in rows if r.get("runtime", "")[11:13] in hours]
                if not keep:
                    print(f"  GAP: {model} {st.mos_station} {a}..{b}: no {sorted(hours)}Z rows "
                          f"({len(rows)} rows total)", flush=True)
                    path.write_text("")          # recorded as a gap, not re-requested
                    continue
                with path.open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(keep[0].keys()))
                    w.writeheader()
                    w.writerows(keep)


def fetch_truth() -> None:
    for st in STATIONS.values():
        path = ROOT / "truth" / f"{st.mos_station}.json"
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"truth {st.mos_station}", flush=True)
        data = fetch_daily_max(st.ghcn_id, START, END)
        path.write_text(json.dumps({d.isoformat(): v for d, v in data.items()}))
        time.sleep(3)


def fetch_kalshi() -> None:
    """Settled markets per series (strikes, result), then the bid/ask candles
    around the lead-1 decision time for each."""
    for series in STATIONS:
        mpath = ROOT / "kalshi" / f"markets_{series}.json"
        if not mpath.exists():
            mpath.parent.mkdir(parents=True, exist_ok=True)
            markets, cursor = [], ""
            for base in ("historical/markets", "markets"):
                cursor = ""
                while True:
                    q = {"series_ticker": series, "limit": 1000}
                    if base == "markets":
                        q["status"] = "settled"
                    if cursor:
                        q["cursor"] = cursor
                    body = _paced(f"{KALSHI}/{base}?{urllib.parse.urlencode(q)}", pause=0.3)
                    d = json.loads(body)
                    markets += d.get("markets", [])
                    cursor = d.get("cursor") or ""
                    if not cursor or not d.get("markets"):
                        break
            keep = {}
            for m in markets:
                keep[m["ticker"]] = {k: m.get(k) for k in (
                    "ticker", "event_ticker", "strike_type", "floor_strike", "cap_strike",
                    "result", "created_time", "close_time", "status")}
            mpath.write_text(json.dumps(list(keep.values())))
            print(f"{series}: {len(keep)} markets", flush=True)

        cpath = ROOT / "kalshi" / f"candles_{series}.json"
        candles = json.loads(cpath.read_text()) if cpath.exists() else {}
        todo = []
        for m in json.loads(mpath.read_text()):
            target = _target_date(m["event_ticker"])
            if (m["ticker"] not in candles and m.get("result") in ("yes", "no")
                    and target is not None and MARKET_START <= target <= END
                    and target.toordinal() % SAMPLE_EVERY_DAYS == 0):
                todo.append((m, target))
        print(f"{series}: {len(todo)} markets need candles", flush=True)

        def one(item):
            m, target = item
            # Lead-1 decision: the hour ending 17:00 UTC on the day before the
            # target (MEX 12Z is out; the session trades 15-21 UTC).
            day = target - dt.timedelta(days=1)
            t0 = int(dt.datetime(day.year, day.month, day.day, 16, tzinfo=dt.timezone.utc).timestamp())
            q = f"start_ts={t0}&end_ts={t0 + 2 * 3600}&period_interval=60"
            # Recent markets live on the main endpoint, older ones under
            # historical/; ask the likelier one first.
            bases = [f"historical/markets/{m['ticker']}", f"series/{series}/markets/{m['ticker']}"]
            if (END - target).days < 60:
                bases.reverse()
            for base in bases:
                wait = 1.0
                for _ in range(8):
                    code, body = _get(f"{KALSHI}/{base}/candlesticks?{q}", timeout=30)
                    if code == 200:
                        time.sleep(0.3)
                        return m["ticker"], json.loads(body).get("candlesticks", [])
                    if code != 429:
                        break                      # 404: try the other endpoint
                    # Exponential backoff, bounded (Developer Agreement 4.3).
                    time.sleep(wait)
                    wait = min(wait * 2, 60)
            return m["ticker"], None

        with ThreadPoolExecutor(max_workers=2) as pool:
            for i, (ticker, data) in enumerate(pool.map(one, todo)):
                candles[ticker] = data
                if i % 500 == 499:
                    cpath.write_text(json.dumps(candles))
                    print(f"  {series}: {i + 1}/{len(todo)}", flush=True)
        cpath.write_text(json.dumps(candles))


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _target_date(event_ticker: str):
    """KXHIGHNY-25OCT14 -> 2025-10-14."""
    try:
        code = event_ticker.split("-")[1]
        return dt.date(2000 + int(code[:2]), _MONTHS[code[2:5]], int(code[5:7]))
    except (IndexError, KeyError, ValueError):
        return None


if __name__ == "__main__":
    {"iem": fetch_iem, "truth": fetch_truth, "kalshi": fetch_kalshi}[sys.argv[1]]()
