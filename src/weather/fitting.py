"""Persisted fits, staleness, and the live regime guard.

Three separate reasons a cell may not price, kept distinct because they need
different responses:

  NOT PROMOTED  it never cleared the held-out gate. Fix by refitting and
                re-running the gate, not by relaxing anything.
  STALE         the fit is older than its refit cadence. The validated design
                refits on a trailing 90-day window before pricing, so a fit
                from three weeks ago is not the thing that was validated —
                pricing off it is pricing off parameters nobody measured.
  DEGRADED      it passed offline and is failing live. Retrospective evidence
                does not survive contact with a regime change.

The last one is the regime guard. Nothing like it existed anywhere in the
codebase — the only prior "pause" is the daily-loss limit — so it is built here
generally enough for other models to reuse.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Engine, select

from src.database import get_session
from src.models.trade import Trade
from src.models.weather import WeatherCellFit
from src.weather.calibration import CellFit, Pair, fit_trailing, score_rolling
from src.weather.calibration import (
    brier as brier_score,
    brier_skill,
    climatology_rate,
    reliability_bins,
    reliability_slope,
)
from src.weather.promotion import MIN_PAIRS_PER_CELL, evaluate_promotion

logger = logging.getLogger(__name__)

# The validated design: refit on a trailing 90-day window before pricing.
REFIT_WINDOW_DAYS = 90
# How old a fit may be before it stops being the thing that was validated.
MAX_FIT_AGE_DAYS = 7

# --- Regime guard ---------------------------------------------------------
# Rolling window of settled paper trades per cell. Below this there is not
# enough live evidence to overrule the offline gate in either direction.
GUARD_MIN_SETTLED = 20
# Predicting 0.5 blindly scores 0.25. Sustained worse than this means the model
# is not merely unskilled but actively misinformed, so the cell pauses.
GUARD_MAX_BRIER = 0.30


@dataclass(frozen=True)
class StoredFit:
    station: str
    lead_days: int
    bias: float
    sigma: float
    promoted: bool
    brier_skill: Optional[float]
    reliability_slope: Optional[float]
    n_eval_pairs: int
    fitted_at: dt.datetime
    rejection_reasons: str = ""

    def as_cell_fit(self) -> CellFit:
        return CellFit(
            station=self.station, lead_days=self.lead_days, bias=self.bias,
            sigma=self.sigma, n_pairs=self.n_eval_pairs,
            fit_start=dt.date.min, fit_end=dt.date.min,
        )

    def age_days(self, now: Optional[dt.datetime] = None) -> float:
        now = now or dt.datetime.now(dt.timezone.utc)
        fitted = self.fitted_at
        if fitted.tzinfo is None:
            fitted = fitted.replace(tzinfo=dt.timezone.utc)
        return (now - fitted).total_seconds() / 86400.0


def save_fit(
    engine: Engine,
    fit: CellFit,
    promoted: bool,
    reasons: Sequence[str],
    brier_skill_value: Optional[float],
    slope: Optional[float],
    n_eval_pairs: int,
    brier_value: Optional[float] = None,
    predictor: str = "mos",
) -> None:
    with get_session(engine) as session:
        row = (
            session.query(WeatherCellFit)
            .filter_by(station=fit.station, lead_days=fit.lead_days, predictor=predictor)
            .first()
        )
        if row is None:
            row = WeatherCellFit(
                station=fit.station, lead_days=fit.lead_days, predictor=predictor,
            )
            session.add(row)
        row.bias = fit.bias
        row.sigma = fit.sigma
        row.n_fit_pairs = fit.n_pairs
        row.n_eval_pairs = n_eval_pairs
        row.brier = brier_value
        row.brier_skill = brier_skill_value
        row.reliability_slope = slope
        row.promoted = promoted
        row.rejection_reasons = "; ".join(reasons) if reasons else None
        row.fit_start = fit.fit_start
        row.fit_end = fit.fit_end
        row.fitted_at = dt.datetime.now(dt.timezone.utc)
        session.commit()


def load_fit(
    engine: Engine, station: str, lead_days: int, predictor: str = "mos",
) -> Optional[StoredFit]:
    with get_session(engine) as session:
        row = (
            session.query(WeatherCellFit)
            .filter_by(station=station, lead_days=lead_days, predictor=predictor)
            .first()
        )
        if row is None:
            return None
        return StoredFit(
            station=row.station, lead_days=row.lead_days, bias=row.bias,
            sigma=row.sigma, promoted=bool(row.promoted),
            brier_skill=row.brier_skill, reliability_slope=row.reliability_slope,
            n_eval_pairs=row.n_eval_pairs or 0, fitted_at=row.fitted_at,
            rejection_reasons=row.rejection_reasons or "",
        )


def all_fits(engine: Engine, predictor: str = "mos") -> List[StoredFit]:
    with get_session(engine) as session:
        rows = session.query(WeatherCellFit).filter_by(predictor=predictor).all()
        return [
            StoredFit(
                station=r.station, lead_days=r.lead_days, bias=r.bias, sigma=r.sigma,
                promoted=bool(r.promoted), brier_skill=r.brier_skill,
                reliability_slope=r.reliability_slope,
                n_eval_pairs=r.n_eval_pairs or 0, fitted_at=r.fitted_at,
                rejection_reasons=r.rejection_reasons or "",
            )
            for r in rows
        ]


# --------------------------------------------------------------------------
# live regime guard
# --------------------------------------------------------------------------

def live_brier(engine: Engine, station_prefix: str, window: int = GUARD_MIN_SETTLED):
    """Rolling Brier over recently settled paper trades for one series.

    The stated probability is the model's chance of the side actually taken, so
    a NO bet is scored on 1 - p_model. Otherwise a confident, correct NO looks
    like a confident, wrong YES.
    """
    with get_session(engine) as session:
        rows = session.execute(
            select(Trade.p_model, Trade.side, Trade.realized_pnl)
            .where(Trade.status == "closed")
            .where(Trade.is_paper.is_(True))
            .where(Trade.market_id.like(f"{station_prefix}%"))
            .order_by(Trade.id.desc())
            .limit(window)
        ).all()

    scored = [
        ((p if side == "yes" else 1.0 - p), 1 if (pnl or 0) > 0 else 0)
        for p, side, pnl in rows
        if p is not None
    ]
    if len(scored) < window:
        return None, len(scored)
    return brier_score([s[0] for s in scored], [s[1] for s in scored]), len(scored)


def guard_paused(
    engine: Engine, series_ticker: str, window: int = GUARD_MIN_SETTLED,
) -> Tuple[bool, str]:
    """Should this series stop pricing on live evidence?

    Offline validation is retrospective. A regime change makes a cell that
    cleared the gate wrong today, and nothing else in the system would notice
    until the drawdown did.
    """
    value, n = live_brier(engine, series_ticker, window)
    if value is None:
        return False, f"only {n}/{window} settled trades — guard not yet active"
    if value > GUARD_MAX_BRIER:
        return True, (
            f"live Brier {value:.3f} over last {n} settled trades exceeds "
            f"{GUARD_MAX_BRIER} (blind 0.5 scores 0.25) — cell paused"
        )
    return False, f"live Brier {value:.3f} over {n} settled trades"


# --------------------------------------------------------------------------
# priceability
# --------------------------------------------------------------------------

def cell_priceable(
    fit: Optional[StoredFit], now: Optional[dt.datetime] = None,
) -> Tuple[bool, str]:
    """May this (station, lead) produce a price right now?"""
    if fit is None:
        return False, "no fit on record for this cell"
    if not fit.promoted:
        return False, f"cell not promoted: {fit.rejection_reasons or 'gate not cleared'}"
    if fit.n_eval_pairs < MIN_PAIRS_PER_CELL:
        return False, (
            f"below sample floor: {fit.n_eval_pairs} held-out pairs "
            f"< {MIN_PAIRS_PER_CELL}"
        )
    age = fit.age_days(now)
    if age > MAX_FIT_AGE_DAYS:
        return False, (
            f"fit is {age:.1f} days old (cadence {MAX_FIT_AGE_DAYS}d) — the "
            f"validated design refits on a trailing window before pricing, so "
            f"these parameters are not the ones that were measured"
        )
    return True, "priceable"


def refit_cell(
    engine: Engine,
    pairs: Sequence[Pair],
    station: str,
    lead_days: int,
    split_date: dt.date,
    predictor: str = "mos",
) -> Optional[StoredFit]:
    """Refit one cell on a trailing window and re-run the unchanged gate."""
    ordered = sorted(pairs, key=lambda p: p.target_date)
    fit_pairs = [p for p in ordered if p.target_date < split_date]
    eval_pairs = [p for p in ordered if p.target_date > split_date]
    if not fit_pairs or not eval_pairs:
        return None

    scored, windows, skipped = score_rolling(
        ordered, eval_pairs, window_days=REFIT_WINDOW_DAYS,
        min_window_pairs=MIN_PAIRS_PER_CELL,
    )
    n_scored = len(scored.pit)
    if n_scored == 0:
        return None

    clim = climatology_rate(fit_pairs)
    bins = reliability_bins(scored.predictions, scored.outcomes)
    slope = reliability_slope(bins)
    bs = brier_score(scored.predictions, scored.outcomes)
    bss = brier_skill(scored.predictions, scored.outcomes, clim)
    verdict = evaluate_promotion(n_scored, bss, slope)

    # The fit that will actually PRICE is the most recent trailing window, not
    # the one used for scoring history.
    latest = fit_trailing(
        ordered, ordered[-1].target_date + dt.timedelta(days=1), REFIT_WINDOW_DAYS,
    )
    if latest is None:
        return None

    save_fit(
        engine, latest, verdict.promoted, verdict.reasons, bss, slope,
        n_scored, brier_value=bs, predictor=predictor,
    )
    logger.info(
        "Refit %s lead %d: BSS=%.3f slope=%.3f promoted=%s (skipped %d thin days)",
        station, lead_days, bss, slope, verdict.promoted, skipped,
    )
    return load_fit(engine, station, lead_days, predictor)


# --------------------------------------------------------------------------
# guard transitions
# --------------------------------------------------------------------------

def sync_guard_state(engine: Engine, scope: str, paused: bool, brier: Optional[float]):
    """Record the current pause state and report whether it just changed.

    Returns (changed, transitions). Transitions accumulate so flapping is
    visible: a cell crossing the threshold repeatedly means the bar is sitting
    on top of the live Brier rather than safely away from it, and that is a
    different problem from a cell that failed once and stayed failed.
    """
    from src.models.weather import GuardState

    with get_session(engine) as session:
        row = session.query(GuardState).filter_by(scope=scope).first()
        if row is None:
            row = GuardState(scope=scope, paused=False, transitions=0)
            session.add(row)
            session.flush()

        changed = bool(row.paused) != bool(paused)
        if changed:
            row.paused = paused
            row.transitions = (row.transitions or 0) + 1
            row.changed_at = dt.datetime.now(dt.timezone.utc)
        row.last_brier = brier
        transitions = row.transitions or 0
        session.commit()
    return changed, transitions


def guard_events(engine: Engine, series_tickers) -> List[dict]:
    """Pause/unpause transitions since the last cycle, ready to alert on."""
    events: List[dict] = []
    for series in series_tickers:
        paused, reason = guard_paused(engine, series)
        value, n = live_brier(engine, series)
        changed, transitions = sync_guard_state(engine, series, paused, value)
        if changed:
            events.append({
                "series": series,
                "paused": paused,
                "brier": value,
                "settled": n,
                "transitions": transitions,
                "reason": reason,
            })
    return events
