"""The live model, and the four independent ways it declines to price.

Everything the offline harness enforced is enforced again here, because the
harness is not what trades. Each gate is tested in isolation AND tested not to
be substitutable by another — a cell that fails must not be rescued by a
neighbour that passes.

None of these produce a downweighted price. A broken input is not a
low-confidence input: the failure this model is most exposed to reads as the
largest edge the system has ever seen.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import Market, TERMS_PARSED, TERMS_UNPARSED
from src.models.price import PriceSnapshot
from src.models.trade import Trade
from src.weather.calibration import CellFit
from src.weather.fitting import (
    GUARD_MAX_BRIER,
    GUARD_MIN_SETTLED,
    MAX_FIT_AGE_DAYS,
    cell_priceable,
    guard_paused,
    load_fit,
    save_fit,
)
from src.weather.model import WeatherModel, target_date_from_ticker
from src.weather.mos import MosForecast
from src.weather.promotion import MIN_PAIRS_PER_CELL

TODAY = dt.datetime(2026, 8, 11, 12, tzinfo=dt.timezone.utc)
TARGET = dt.date(2026, 8, 12)          # lead 1
TICKER = "KXHIGHNY-26AUG12-T90"


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _seed_market(engine, ticker=TICKER, direction="above", strike=90.0,
                 status=TERMS_PARSED, series="KXHIGHNY", bid=40, ask=44):
    with get_session(engine) as s:
        s.add(Market(
            market_id=ticker, title="Will the high temp in NYC be >90°?",
            category="Climate and Weather",
            close_date=dt.datetime(2026, 8, 13, 4, 59, tzinfo=dt.timezone.utc),
            status="open", series_ticker=series,
            strike_direction=direction, strike_value=strike,
            strike_unit="F", terms_status=status,
        ))
        s.add(PriceSnapshot(
            market_id=ticker, yes_bid=bid, yes_ask=ask, last_price=42, volume=500,
        ))
        s.commit()


def _seed_fit(engine, station="KNYC", lead=1, promoted=True, bias=0.4, sigma=3.2,
              n_eval=105, skill=0.62, slope=1.03, age_days=0.0):
    fit = CellFit(station, lead, bias, sigma, 160, dt.date(2025, 4, 1), dt.date(2025, 8, 31))
    save_fit(engine, fit, promoted, [] if promoted else ["gate not cleared"],
             skill, slope, n_eval)
    if age_days:
        from src.models.weather import WeatherCellFit
        with get_session(engine) as s:
            row = s.query(WeatherCellFit).filter_by(station=station, lead_days=lead).first()
            row.fitted_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
            s.commit()


class _Mos:
    """Stands in for the MOS endpoint."""

    def __init__(self, temp=93.0):
        self.temp = temp

    def get(self, url, params=None, timeout=None):
        from unittest.mock import MagicMock
        rows = []
        for lead in range(1, 8):
            target = TODAY.date() + dt.timedelta(days=lead)
            rows.append({"ftime": f"{target + dt.timedelta(days=1)} 00:00",
                         "n_x": self.temp})
            rows.append({"ftime": f"{target} 12:00", "n_x": self.temp - 20})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": rows}
        return resp


def _model(temp=93.0):
    return WeatherModel(http=_Mos(temp), now=lambda: TODAY)


# --------------------------------------------------------------------------
# 1. Ticker date, and the happy path
# --------------------------------------------------------------------------

def test_target_date_read_from_ticker_not_close_time():
    assert target_date_from_ticker(TICKER) == dt.date(2026, 8, 12)
    assert target_date_from_ticker("KXHIGHAUS-26DEC01-T50") == dt.date(2026, 12, 1)
    assert target_date_from_ticker("garbage") is None


def test_promoted_cell_prices(engine):
    _seed_market(engine)
    _seed_fit(engine)
    result = _model(temp=93.0).estimate(TICKER, "t", 0.42, engine)
    assert result is not None
    assert 0.0 <= result.p_model <= 1.0
    assert result.p_model > 0.5          # forecast 93.4 vs strike 90
    assert result.data_sources == ["nws_mos"]
    assert "held-out BSS" in result.reasoning


def test_confidence_tracks_measured_skill_of_that_cell(engine):
    _seed_market(engine)
    _seed_fit(engine, skill=0.62)
    strong = _model().estimate(TICKER, "t", 0.42, engine).confidence

    with get_session(engine) as s:
        from src.models.weather import WeatherCellFit
        s.query(WeatherCellFit).filter_by(station="KNYC").first().brier_skill = 0.10
        s.commit()
    weak = _model().estimate(TICKER, "t", 0.42, engine).confidence

    assert strong > weak
    assert weak >= 0.5 and strong <= 0.85


# --------------------------------------------------------------------------
# 2. Per-cell promotion, floor, staleness
# --------------------------------------------------------------------------

class TestCellGates:
    def test_unpromoted_cell_does_not_price(self, engine):
        _seed_market(engine)
        _seed_fit(engine, promoted=False)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_below_floor_cell_does_not_price(self, engine):
        _seed_market(engine)
        _seed_fit(engine, n_eval=MIN_PAIRS_PER_CELL - 1)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_stale_fit_does_not_price_on_old_parameters(self, engine):
        """The validated design refits on a trailing window before pricing, so
        an old fit is not the thing that was validated."""
        _seed_market(engine)
        _seed_fit(engine, age_days=MAX_FIT_AGE_DAYS + 1)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_fresh_fit_inside_cadence_still_prices(self, engine):
        _seed_market(engine)
        _seed_fit(engine, age_days=MAX_FIT_AGE_DAYS - 1)
        assert _model().estimate(TICKER, "t", 0.42, engine) is not None

    def test_missing_cell_does_not_price(self, engine):
        _seed_market(engine)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_promotion_is_per_cell_not_global(self, engine):
        """A promoted lead-1 cell must not let an unpromoted lead-2 price."""
        _seed_market(engine)
        _seed_fit(engine, lead=1, promoted=True)
        _seed_fit(engine, lead=2, promoted=False)
        assert _model().estimate(TICKER, "t", 0.42, engine) is not None

        far = "KXHIGHNY-26AUG13-T90"
        _seed_market(engine, ticker=far)
        assert _model().estimate(far, "t", 0.42, engine) is None

    def test_lead_beyond_validated_range_does_not_price(self, engine):
        """Borrowing lead 3's sigma for lead 6 understates the error badly."""
        far = "KXHIGHNY-26AUG20-T90"
        _seed_market(engine, ticker=far)
        _seed_fit(engine, lead=1)
        assert _model().estimate(far, "t", 0.42, engine) is None


# --------------------------------------------------------------------------
# 3. Terms must have parsed
# --------------------------------------------------------------------------

class TestTermsGate:
    def test_unparsed_terms_do_not_price(self, engine):
        _seed_market(engine, status=TERMS_UNPARSED, direction=None, strike=None)
        _seed_fit(engine)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_below_contract_uses_the_below_tail(self, engine):
        _seed_market(engine, ticker="KXHIGHNY-26AUG12-T99",
                     direction="below", strike=99.0)
        _seed_fit(engine)
        result = _model(temp=93.0).estimate("KXHIGHNY-26AUG12-T99", "t", 0.5, engine)
        assert result is not None
        assert result.p_model > 0.5      # 93.4 is comfortably below 99


# --------------------------------------------------------------------------
# 4. Sanity tripwire at prediction time
# --------------------------------------------------------------------------

class TestTripwireAtPredictionTime:
    def _seed_ladder(self, engine, implied_center=88):
        """A coherent ladder whose implied median sits near `implied_center`."""
        for offset, (bid, ask) in zip(
            (-4, -2, 0, 2, 4), ((88, 92), (68, 72), (48, 52), (28, 32), (8, 12)),
        ):
            _seed_market(
                engine, ticker=f"KXHIGHNY-26AUG12-T{implied_center + offset}",
                strike=float(implied_center + offset), bid=bid, ask=ask,
            )

    def test_overnight_minimum_signature_is_refused(self, engine):
        """~20°F below the whole ladder. Presents as enormous edge."""
        self._seed_ladder(engine)
        _seed_fit(engine)
        target = "KXHIGHNY-26AUG12-T88"
        assert _model(temp=68.0).estimate(target, "t", 0.5, engine) is None

    def test_ordinary_disagreement_still_prices(self, engine):
        self._seed_ladder(engine)
        _seed_fit(engine)
        target = "KXHIGHNY-26AUG12-T88"
        assert _model(temp=93.0).estimate(target, "t", 0.5, engine) is not None


# --------------------------------------------------------------------------
# 5. Regime guard — live evidence overriding offline validation
# --------------------------------------------------------------------------

class TestRegimeGuard:
    def _settle(self, engine, n, p_model, won, series="KXHIGHNY"):
        with get_session(engine) as s:
            for i in range(n):
                s.add(Trade(
                    market_id=f"{series}-26AUG{i:02d}-T90", side="yes", action="buy",
                    price=50, quantity=1, p_model=p_model, implied_prob=0.5,
                    edge=0.1, net_ev=0.05, position_size_dollars=0.5,
                    confidence=0.8, reasoning="t", is_paper=True, status="closed",
                    realized_pnl=(1.0 if won else -1.0), model_name="WeatherModel",
                ))
            s.commit()

    def test_guard_inactive_until_enough_settled_trades(self, engine):
        self._settle(engine, GUARD_MIN_SETTLED - 1, 0.9, won=False)
        paused, reason = guard_paused(engine, "KXHIGHNY")
        assert not paused
        assert "not yet active" in reason

    def test_confidently_wrong_cell_is_paused(self, engine):
        """Offline validation is retrospective; a regime change makes a cell
        that cleared the gate wrong today."""
        self._settle(engine, GUARD_MIN_SETTLED, 0.95, won=False)
        paused, reason = guard_paused(engine, "KXHIGHNY")
        assert paused
        assert "paused" in reason

    def test_well_performing_cell_is_not_paused(self, engine):
        self._settle(engine, GUARD_MIN_SETTLED, 0.9, won=True)
        paused, _ = guard_paused(engine, "KXHIGHNY")
        assert not paused

    def test_no_side_is_scored_on_its_own_probability(self, engine):
        """A confident, correct NO must not look like a confident, wrong YES."""
        with get_session(engine) as s:
            for i in range(GUARD_MIN_SETTLED):
                s.add(Trade(
                    market_id=f"KXHIGHNY-26AUG{i:02d}-T90", side="no", action="buy",
                    price=50, quantity=1, p_model=0.05, implied_prob=0.5,
                    edge=0.1, net_ev=0.05, position_size_dollars=0.5,
                    confidence=0.8, reasoning="t", is_paper=True, status="closed",
                    realized_pnl=1.0, model_name="WeatherModel",
                ))
            s.commit()
        paused, _ = guard_paused(engine, "KXHIGHNY")
        assert not paused

    def test_paused_cell_produces_no_estimate(self, engine):
        _seed_market(engine)
        _seed_fit(engine)
        self._settle(engine, GUARD_MIN_SETTLED, 0.95, won=False)
        assert _model().estimate(TICKER, "t", 0.42, engine) is None

    def test_guard_is_per_series(self, engine):
        """Denver degrading must not silence New York."""
        self._settle(engine, GUARD_MIN_SETTLED, 0.95, won=False, series="KXHIGHDEN")
        assert guard_paused(engine, "KXHIGHDEN")[0]
        assert not guard_paused(engine, "KXHIGHNY")[0]


# --------------------------------------------------------------------------
# 6. Priceability reasons stay distinguishable
# --------------------------------------------------------------------------

def test_each_refusal_reports_its_own_reason(engine):
    """A refit failure and a failed gate need different responses, so the
    digest must be able to tell them apart."""
    from src.weather.fitting import StoredFit

    now = dt.datetime.now(dt.timezone.utc)
    fresh = dict(station="KNYC", lead_days=1, bias=0.0, sigma=3.0,
                 brier_skill=0.5, reliability_slope=1.0, fitted_at=now)

    assert "no fit" in cell_priceable(None)[1]
    assert "not promoted" in cell_priceable(
        StoredFit(promoted=False, n_eval_pairs=105, **fresh))[1]
    assert "sample floor" in cell_priceable(
        StoredFit(promoted=True, n_eval_pairs=10, **fresh))[1]
    stale = dict(fresh, fitted_at=now - dt.timedelta(days=MAX_FIT_AGE_DAYS + 2))
    assert "days old" in cell_priceable(
        StoredFit(promoted=True, n_eval_pairs=105, **stale))[1]
