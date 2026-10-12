"""Live size ramp (approved 2026-10-12; docs/size-ramp.md). Tests first.

The ramp is a min() applied after every existing limit, in live mode only. It
can only lower a trade's size; nothing here may raise a cap, touch a limit,
or flip a mode.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.risk.ramp as ramp
from src.database import Base, get_session
from src.ev.calculator import EVResult
from src.models.settings import TradingSettings
from src.risk.manager import RiskManager

NOW = dt.datetime(2026, 10, 13, 16, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings(bankroll=1000.0, peak_bankroll=1000.0, mode="paper"))
        s.commit()
    return db_engine


def _mode(engine, mode):
    with get_session(engine) as s:
        s.query(TradingSettings).first().mode = mode
        s.commit()


def _ev(p=0.70, price=0.30):
    return EVResult(p_model=p, implied_prob=price, edge=p - price, no_edge=price - p,
                    raw_ev=0.3, net_ev=0.3, no_ev=-0.3, recommended_side="yes", fee_rate=0.0)


def _quantity(engine):
    d = RiskManager(engine).evaluate(ev_result=_ev(), confidence=0.9,
                                     market_id="KXHIGHNY-26OCT13-T70", market_category="Weather")
    assert d.approved, d.rejection_reasons
    return d


class TestCaps:
    def test_paper_mode_is_untouched(self, engine):
        assert _quantity(engine).quantity > 1

    def test_stage_zero_live_trade_is_one_contract(self, engine):
        risk_q = _quantity(engine).quantity
        _mode(engine, "live")
        d = _quantity(engine)
        assert risk_q > 1 and d.quantity == 1
        assert d.position_size_dollars == pytest.approx(d.price_cents / 100)

    @pytest.mark.parametrize("stage", [0, 1, 2, 3])
    def test_the_ramp_never_exceeds_what_risk_approved(self, stage):
        for q in range(0, 300):
            assert ramp.ramped_quantity(q, stage) <= q

    def test_the_stage_caps_are_the_approved_ones(self):
        assert ramp.STAGE_CAPS == {0: 1, 1: 3, 2: 10, 3: None}
        assert ramp.STAGE_MIN_TRADES == {0: 20, 1: 20, 2: 40}

    def test_the_hard_limits_did_not_move(self, engine):
        with get_session(engine) as s:
            st = s.query(TradingSettings).first()
            assert (st.kelly_fraction, st.max_single_trade_pct,
                    st.max_total_exposure_pct, st.drawdown_circuit_breaker_pct) == (0.25, 0.03, 0.25, 0.20)


class TestPromotion:
    def test_promotion_is_refused_while_a_bar_is_unmet(self, engine):
        _mode(engine, "live")
        ok, bars = ramp.eligibility(engine)
        assert not ok
        assert not ramp.promote(engine, by="test")
        assert ramp.current_stage(engine) == 0

    def test_ledger_parity_fails_closed_until_a_reconciler_exists(self, engine):
        _, bars = ramp.eligibility(engine)
        parity = dict((name, (passed, detail)) for name, passed, detail in bars)["ledger_parity"]
        assert parity[0] is False and "reconcil" in parity[1]

    def test_with_every_bar_met_a_promotion_moves_one_stage(self, engine, monkeypatch):
        monkeypatch.setattr(ramp, "eligibility", lambda e, now=None: (True, []))
        assert ramp.promote(engine, by="test")
        assert ramp.current_stage(engine) == 1

    def test_the_token_gate(self, engine, monkeypatch, capsys):
        from tests.test_maintenance_entrypoint import run_module

        code, out = run_module(["--promote-ramp"], engine, monkeypatch, capsys)
        assert code == 0 and "stage 0" in out                         # dry run
        code, _ = run_module(["--promote-ramp", "--confirm", "CLEAR-HALT"], engine, monkeypatch, capsys)
        assert code == 2
        code, _ = run_module(["--promote-ramp", "--confirm", "PROMOTE-RAMP"], engine, monkeypatch, capsys)
        assert code == 1 and ramp.current_stage(engine) == 0           # bars unmet: refused


class TestDemotion:
    def test_any_kill_switch_trip_returns_the_ramp_to_stage_zero(self, engine, monkeypatch):
        from src.risk.halts import trip

        monkeypatch.setattr(ramp, "eligibility", lambda e, now=None: (True, []))
        ramp.promote(engine, by="test")
        ramp.promote(engine, by="test")
        assert ramp.current_stage(engine) == 2

        trip(engine, "bankroll_drop", "test", alert=lambda m: None, now=NOW)

        assert ramp.current_stage(engine) == 0
        _mode(engine, "live")
        assert _quantity(engine).quantity == 1
