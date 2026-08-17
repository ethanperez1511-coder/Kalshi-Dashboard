"""The weather model, and the four separate ways it declines to price.

Every gate that was enforced in the offline harness is enforced again here, at
prediction time, because the harness is not what trades:

  terms       the contract must have parsed. An unread contract has no
              direction and no strike, so there is nothing to price.
  cell        the (station, lead) must be promoted, above the sample floor, and
              fitted recently enough to still be the thing that was validated.
  guard       live settled results must not have degraded past tolerance.
  sanity      the forecast must not disagree with the market by more than
              weather ever does.

Any one of them failing produces NO estimate. None of them produce a widened
spread, a lower size, or a downweighted probability — a broken input is not a
low-confidence input, and the failure this model is most exposed to (an
overnight minimum read as a daily maximum) presents as the largest edge the
system has ever seen.

Dispersion is estimated from historical forecast error, not observed. The MOS
forecast carries no uncertainty of its own, so this cannot distinguish a
confident day from an uncertain one; that is the known limitation and the
reason an ensemble challenger is queued behind its probe.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from collections import Counter
from typing import List, Optional

from sqlalchemy import Engine, select

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult, MODEL_TYPE_INDEPENDENT
from src.models.market import Market, TERMS_PARSED
from src.models.price import PriceSnapshot
from src.weather import mos
from src.weather.fitting import cell_priceable, guard_paused, load_fit
from src.weather.sanity import LadderPoint, check_forecast
from src.weather.settlement_guard import verify_settlement
from src.weather.stations import station_for_market

logger = logging.getLogger(__name__)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")

# Leads the fits are validated over. A contract further out has no measured
# cell, and an unmeasured lead is not a small extrapolation — forecast error
# grows fast enough that borrowing lead 3's sigma for lead 6 understates it.
MAX_PRICEABLE_LEAD = 3


def target_date_from_ticker(market_id: str) -> Optional[dt.date]:
    """`KXHIGHNY-26AUG12-T90` -> 2026-08-12.

    Read from the ticker rather than derived from close_time: these contracts
    close the following morning UTC, so deriving the date means re-deriving a
    timezone rule that the ticker already states outright.
    """
    match = _TICKER_DATE.search(market_id or "")
    if not match:
        return None
    year, month, day = match.groups()
    month_num = _MONTHS.get(month.upper())
    if month_num is None:
        return None
    try:
        return dt.date(2000 + int(year), month_num, int(day))
    except ValueError:
        return None


class WeatherModel(BaseModel):
    """Prices daily temperature contracts from calibrated MOS guidance."""

    def __init__(self, http=None, now=None):
        self._http = http
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        # Per-cycle caches. Each of these was a query — or, for MOS, an HTTP
        # fetch — repeated for every market being scored. The registry builds
        # one model per scoring run, so instance scope is cycle scope.
        self._terms_cache = None
        self._fit_cache = {}
        self._guard_cache = {}
        self._forecast_cache = {}
        self._ladder_cache = {}
        # Why the model declined, counted. Four independent gates can each
        # produce silence, and "0 of 28 priced" does not say which one fired —
        # a stale fit and a tripwire trip are the same number in the digest and
        # completely different situations.
        self.refusals: Counter = Counter()

    @property
    def category(self) -> str:
        return "Climate and Weather"

    def matches(self, category: str) -> bool:
        return category in ("Climate and Weather", "Weather")

    def claims(self, market_id: str, category: str) -> bool:
        """Scope is the station map, not the category string.

        Measured against the live API on 2026-08-13: all 84 daily temperature
        contracts are returned with category "General". Routing on category
        meant this model was dispatched to 26 unrelated "Climate and Weather"
        markets it has no station for, and to none of the 28 it can price. The
        station map is the honest scope and it is the same lookup `estimate`
        already performs first, so this cannot claim a market it would refuse.
        """
        return station_for_market(market_id) is not None or self.matches(category)

    @property
    def model_type(self) -> str:
        return MODEL_TYPE_INDEPENDENT

    # -- helpers ---------------------------------------------------------

    def _terms(self, engine: Engine, market_id: str):
        """Contract terms for every parsed threshold market, loaded once."""
        if self._terms_cache is None:
            with get_session(engine) as session:
                rows = session.execute(
                    select(
                        Market.market_id, Market.terms_status,
                        Market.strike_direction, Market.strike_value,
                        Market.series_ticker, Market.rules,
                    ).where(Market.terms_status == TERMS_PARSED)
                ).all()
            self._terms_cache = {
                r[0]: {
                    "terms_status": r[1], "direction": r[2],
                    "strike": r[3], "series": r[4], "rules": r[5],
                }
                for r in rows
            }
        return self._terms_cache.get(market_id)

    def _ladder(self, engine: Engine, series: str, target: dt.date) -> List[LadderPoint]:
        """Sibling contracts for the same station-day, with their market prices.

        The ladder is what pins the market's implied median: a whole city-day
        cannot be mispriced coherently, so the siblings together are a far
        better reference than any single contract's price.
        """
        if not series:
            return []
        stamp = f"{target:%y%b%d}".upper()
        with get_session(engine) as session:
            rows = session.execute(
                select(Market.market_id, Market.strike_direction, Market.strike_value)
                .where(Market.series_ticker == series)
                .where(Market.terms_status == TERMS_PARSED)
                .where(Market.market_id.like(f"%-{stamp}-%"))
            ).all()
            points: List[LadderPoint] = []
            for market_id, direction, strike in rows:
                if direction is None or strike is None:
                    continue
                snap = session.execute(
                    select(PriceSnapshot.yes_bid, PriceSnapshot.yes_ask)
                    .where(PriceSnapshot.market_id == market_id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                ).first()
                if not snap or not snap[0] or not snap[1]:
                    continue
                mid = (snap[0] + snap[1]) / 200.0
                points.append(LadderPoint(float(strike), direction, mid))
        return points

    # -- estimate --------------------------------------------------------

    def estimate(
        self, market_id: str, title: str, current_price: float, engine: Engine,
    ) -> Optional[ModelResult]:
        station = station_for_market(market_id)
        if station is None or engine is None:
            self.refusals["no_station_mapping" if engine is not None else "no_engine"] += 1
            return None

        target = target_date_from_ticker(market_id)
        if target is None:
            logger.info("Weather: %s has no readable target date", market_id)
            self.refusals["no_target_date"] += 1
            return None

        terms = self._terms(engine, market_id)
        if terms is None or terms["terms_status"] != TERMS_PARSED:
            self.refusals["terms_not_parsed"] += 1
            return None
        if terms["direction"] is None or terms["strike"] is None:
            self.refusals["terms_incomplete"] += 1
            return None

        today = self._now().date()
        intended_lead = (target - today).days
        if intended_lead < 1 or intended_lead > MAX_PRICEABLE_LEAD:
            self.refusals[
                "lead_past" if intended_lead < 1
                else f"lead_beyond_{MAX_PRICEABLE_LEAD}d"
            ] += 1
            return None

        # Does this contract still settle where and how we think it does?
        # Kalshi repointed all seven series on 2026-08-14 and the only thing
        # that noticed was a CI job, red three days running, while the pricing
        # path knew nothing. A guard that runs only in CI gates CI.
        #
        # Placed AFTER the date checks and before any network call. A contract
        # already past its target is refused either way, and reporting it as a
        # settlement problem would bury the real alarm under every expired
        # ladder rung still sitting in the table. This fires only for contracts
        # that would otherwise have been priced.
        settled_ok, settlement_reason = verify_settlement(
            market_id, terms.get("rules"),
        )
        if not settled_ok:
            logger.error(
                "Weather: %s NOT PRICED — %s. The contract's settlement "
                "description no longer matches the mapped station/authority.",
                market_id, settlement_reason,
            )
            self.refusals[settlement_reason] += 1
            return None

        # Guard first: one cached DB read per series, and it can veto the whole
        # city, so it runs ahead of anything that costs an HTTP call.
        if station.series_ticker not in self._guard_cache:
            self._guard_cache[station.series_ticker] = guard_paused(
                engine, station.series_ticker,
            )
        paused, guard_reason = self._guard_cache[station.series_ticker]
        if paused:
            logger.warning("Weather: %s paused — %s", market_id, guard_reason)
            self.refusals["guard_paused"] += 1
            return None

        # The forecast resolves BEFORE the fit, because until the run is in hand
        # the lead is not known. This used to compute the lead from today and
        # then demand the run at target-minus-lead — always today's 12Z, which
        # does not reach the IEM archive until ~17:30 UTC, so weather refused
        # for roughly 73% of the day's cycles. Now the newest PUBLISHED 12Z run
        # wins and the lead comes from it.
        #
        # One fetch per (station, target) per cycle: a city-day ladder is six
        # contracts sharing one forecast.
        forecast_key = (station.mos_station, target)
        if forecast_key not in self._forecast_cache:
            try:
                self._forecast_cache[forecast_key] = mos.latest_forecast_for(
                    station.mos_station, target, now=self._now(),
                    **({"http": self._http} if self._http is not None else {}),
                )
            except mos.MosUnavailable as exc:
                logger.info("Weather: %s unpriceable — %s", market_id, exc)
                self._forecast_cache[forecast_key] = None
        forecast = self._forecast_cache[forecast_key]
        if forecast is None:
            self.refusals["mos_unavailable"] += 1
            return None

        # The lead the run actually carries. The fallback can push this past the
        # limit even when the target itself is close, and that gets its own
        # counter: "the newest guidance available is too old to price from" is a
        # different situation from a contract that was always too far out.
        lead = forecast.lead_days
        if lead > MAX_PRICEABLE_LEAD:
            logger.info(
                "Weather: %s unpriceable — newest published run is %dd from "
                "target, past the %dd limit",
                market_id, lead, MAX_PRICEABLE_LEAD,
            )
            self.refusals[f"lead_beyond_{MAX_PRICEABLE_LEAD}d_after_fallback"] += 1
            return None

        # The fit for the lead the forecast HAS, never the lead we wanted. A
        # lead-1 sigma on a lead-2 forecast is a precise statement about the
        # wrong distribution, and the number it produces looks exactly like a
        # good one.
        cache_key = (station.mos_station, lead)
        if cache_key not in self._fit_cache:
            self._fit_cache[cache_key] = load_fit(engine, station.mos_station, lead)
        fit_record = self._fit_cache[cache_key]
        ok, reason = cell_priceable(fit_record, self._now())
        if not ok:
            logger.info("Weather: %s unpriceable — %s", market_id, reason)
            self.refusals[f"cell_{reason}"] += 1
            return None

        fit = fit_record.as_cell_fit()
        predicted = fit.predict_mean(forecast.max_temp_f)

        ladder_key = (terms["series"], target)
        if ladder_key not in self._ladder_cache:
            self._ladder_cache[ladder_key] = self._ladder(engine, terms["series"], target)
        verdict = check_forecast(predicted, self._ladder_cache[ladder_key])
        if not verdict.ok:
            logger.error(
                "Weather SUSPECT %s: %s", market_id, verdict.reason,
            )
            self.refusals["sanity_tripwire"] += 1
            return None

        strike = float(terms["strike"])
        if terms["direction"] == "above":
            p_model = fit.prob_above(forecast.max_temp_f, strike)
        else:
            p_model = fit.prob_below(forecast.max_temp_f, strike)

        # Confidence is tied to the measured held-out skill of THIS cell rather
        # than to a constant, and capped below the level reserved for exact
        # multi-source data.
        skill = fit_record.brier_skill or 0.0
        confidence = max(0.5, min(0.85, 0.55 + 0.5 * skill))

        self.refusals["priced"] += 1
        return ModelResult(
            market_id=market_id,
            p_model=max(0.0, min(1.0, p_model)),
            confidence=confidence,
            reasoning=(
                f"MOS {station.mos_station} lead {lead}d: {forecast.max_temp_f:.0f}°F "
                f"{fit.bias:+.2f} bias -> {predicted:.1f}°F, sd {fit.sigma:.2f}; "
                f"P({terms['direction']} {strike:.0f}°) = {p_model:.1%} "
                f"[held-out BSS {skill:.3f}, n={fit_record.n_eval_pairs}]"
            ),
            data_sources=["nws_mos"],
        )
