"""Kalshi's fee schedule, per series, read from Kalshi.

    taker fee = round_up(BASE_TAKER_RATE x multiplier x C x P x (1 - P))
    maker fee = round_up(BASE_MAKER_RATE x multiplier x C x P x (1 - P)),
                only on series whose fee_type carries maker fees

Source: Kalshi's published fee schedule and GET /series/{s} -> fee_type,
fee_multiplier (KXHIGHNY on 2026-10-06: "quadratic", 1). Weather pays the
standard taker fee and no maker fee.

A fee we cannot vouch for is a refusal, never a guess (CLAUDE.md: no fallback
data on the money path). Ruling 2026-10-06 (D): if the fetch fails, a schedule
seen under 24 hours ago is used and named in the digest; otherwise the series
is refused with a Telegram alert. A fee_type the formula does not model
("flat", combo maker fees) is refused outright. A change from the last schedule
seen is alerted with old and new.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import Engine, select

from src.database import get_session
from src.models.fees import SeriesFee
from src.trading_config import KALSHI_FEE_RATE

logger = logging.getLogger(__name__)

BASE_TAKER_RATE = KALSHI_FEE_RATE          # 0.07
BASE_MAKER_RATE = 0.0175
MAX_CACHE_AGE = dt.timedelta(hours=24)

# fee_type -> charges a maker fee. Anything else is not modelled and refused.
SUPPORTED_FEE_TYPES = {"quadratic": False, "quadratic_with_maker_fees": True}


@dataclass(frozen=True)
class FeeSchedule:
    series: str
    fee_type: str
    multiplier: float
    observed_at: dt.datetime
    cached: bool = False

    @property
    def taker_rate(self) -> float:
        return BASE_TAKER_RATE * self.multiplier

    @property
    def maker_rate(self) -> float:
        return BASE_MAKER_RATE * self.multiplier if SUPPORTED_FEE_TYPES.get(self.fee_type) else 0.0

    def fee(self, quantity: int, price_cents: int, maker: bool = False) -> float:
        """Dollars for a fill, rounded up to the cent as Kalshi does."""
        if quantity <= 0 or price_cents <= 0 or price_cents >= 100:
            return 0.0
        rate = self.maker_rate if maker else self.taker_rate
        p = price_cents / 100.0
        return math.ceil(rate * quantity * p * (1.0 - p) * 100.0) / 100.0


def _utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class FeeBook:
    """One cycle's view of fee schedules. Fetches each series at most once per
    cycle and alerts at most once per series per cycle."""

    def __init__(
        self, engine: Engine, fetch: Callable[[str], Tuple[str, float]],
        alert: Callable[[str], object], now: Optional[dt.datetime] = None,
    ):
        self._engine = engine
        self._fetch = fetch
        self._alert = alert
        self._now = now or dt.datetime.now(dt.timezone.utc)
        self._memo: Dict[str, Tuple[Optional[FeeSchedule], Optional[str]]] = {}

    def schedule(self, series: str) -> Tuple[Optional[FeeSchedule], Optional[str]]:
        if series not in self._memo:
            self._memo[series] = self._resolve(series)
            refusal = self._memo[series][1]
            if refusal:
                self._alert(f"🚨 <b>FEE REFUSAL</b> {series}: {refusal} — not trading it")
        return self._memo[series]

    def _resolve(self, series: str) -> Tuple[Optional[FeeSchedule], Optional[str]]:
        now = self._now
        with get_session(self._engine) as session:
            row = session.get(SeriesFee, series)
            known = None if row is None else (row.fee_type, float(row.fee_multiplier), _utc(row.observed_at))

        try:
            fee_type, multiplier = self._fetch(series)
            fee_type, multiplier = str(fee_type), float(multiplier)
            if not multiplier > 0:
                raise ValueError(f"fee_multiplier {multiplier!r} is not positive")
        except Exception as exc:
            self._record_failure(series, f"{exc.__class__.__name__}: {exc}")
            if known is None:
                return None, f"fee schedule unavailable ({exc.__class__.__name__}) and never seen"
            age = now - known[2]
            if age >= MAX_CACHE_AGE:
                return None, (
                    f"fee schedule unavailable and the last one is {age.total_seconds() / 3600:.0f} h "
                    f"old (limit 24 h)"
                )
            schedule = FeeSchedule(series, known[0], known[1], known[2], cached=True)
            return self._check_supported(schedule)

        if known is not None and (known[0], known[1]) != (fee_type, multiplier):
            self._alert(
                f"⚠️ <b>FEE CHANGE</b> {series}: {known[0]} x{known[1]:g} -> "
                f"{fee_type} x{multiplier:g}. Edges are recomputed at the new rate."
            )
        self._record_success(series, fee_type, multiplier)
        return self._check_supported(FeeSchedule(series, fee_type, multiplier, now))

    @staticmethod
    def _check_supported(schedule: FeeSchedule) -> Tuple[Optional[FeeSchedule], Optional[str]]:
        if schedule.fee_type not in SUPPORTED_FEE_TYPES:
            return None, f"fee_type {schedule.fee_type!r} is not one the fee formula models"
        return schedule, None

    def _record_success(self, series: str, fee_type: str, multiplier: float) -> None:
        with get_session(self._engine) as session:
            row = session.get(SeriesFee, series) or SeriesFee(series=series)
            row.fee_type, row.fee_multiplier = fee_type, multiplier
            row.observed_at = row.attempted_at = self._now
            row.last_error = None
            session.merge(row)
            session.commit()

    def _record_failure(self, series: str, error: str) -> None:
        with get_session(self._engine) as session:
            row = session.get(SeriesFee, series)
            if row is not None:              # nothing known: nothing to mark stale
                row.attempted_at, row.last_error = self._now, error[:500]
                session.commit()


def fetch_from_kalshi(base_url: str) -> Callable[[str], Tuple[str, float]]:
    """GET /series/{s}, public. Raises on anything but a well-formed answer."""
    import httpx

    def fetch(series: str) -> Tuple[str, float]:
        response = httpx.get(f"{base_url}/series/{series}", timeout=10)
        response.raise_for_status()
        body = response.json()["series"]
        return body["fee_type"], body["fee_multiplier"]

    return fetch


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def fee_digest(engine: Engine, now: Optional[dt.datetime] = None) -> Dict[str, List]:
    now = now or dt.datetime.now(dt.timezone.utc)
    out: Dict[str, List] = {"live": [], "cached": [], "stale": []}
    with get_session(engine) as session:
        rows = session.execute(select(SeriesFee)).scalars().all()
        for r in rows:
            observed, attempted = _utc(r.observed_at), _utc(r.attempted_at)
            failing = attempted is not None and attempted > observed
            age_h = (now - observed).total_seconds() / 3600
            if not failing:
                out["live"].append(r.series)
            elif now - observed < MAX_CACHE_AGE:
                out["cached"].append((r.series, age_h))
            else:
                out["stale"].append((r.series, age_h))
    return out


def format_fee_digest(data: Dict[str, List]) -> str:
    line = f"💸 Fees: {len(data['live'])} series live from Kalshi"
    if data["cached"]:
        line += " · CACHED: " + ", ".join(f"{s} ({h:.0f}h)" for s, h in sorted(data["cached"]))
    if data["stale"]:
        line += " · REFUSED (>24h): " + ", ".join(f"{s} ({h:.0f}h)" for s, h in sorted(data["stale"]))
    return line
