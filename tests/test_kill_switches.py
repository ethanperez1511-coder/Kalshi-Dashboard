"""Kill switches: latched halts that only a human clears (ruling 2026-10-09).

The existing limits are per-trade refusals recomputed every cycle, so the
drawdown breaker un-trips the moment equity bounces. A latched halt stays on
until a human clears it with a typed token, and every switch below is tripped
by the condition it exists for, shown to stay tripped after the condition
clears, shown to block the next trade, and shown to clear only by token.

Halts refuse NEW trades. They never place or close an order and never touch
limits, sizing, mode or the live gate.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.risk.halts as halts
from src.database import get_session
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.risk.halts import active_halts, check_cycle_halts, clear_halts, trip
from tests.test_shadow_wiring import MARKET, NOW, _Alerter, _qualifying, engine  # noqa: F401


def _set_equity(engine, bankroll, peak):
    with get_session(engine) as s:
        row = s.query(TradingSettings).first()
        row.bankroll, row.peak_bankroll = bankroll, peak
        s.commit()


def _fresh_snapshot(engine, at=NOW):
    with get_session(engine) as s:
        s.add(PriceSnapshot(market_id=MARKET, yes_bid=44, yes_ask=46, last_price=45,
                            volume=10, timestamp=at))
        s.commit()


def _execute(engine, monkeypatch, opps=None):
    import src.run_trading as rt

    monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", False)
    return rt.execute_qualifying(engine, opps or _qualifying(), _Alerter(), now=NOW)


def _trades(engine):
    with get_session(engine) as s:
        return s.query(Trade.id).count()


def _check(engine, now=NOW):
    alerts = []
    return check_cycle_halts(engine, alerts.append, now=now), alerts


# --------------------------------------------------------------------------
# The latch itself
# --------------------------------------------------------------------------

class TestLatch:
    def test_an_active_halt_blocks_every_new_trade(self, engine, monkeypatch):
        trip(engine, "drawdown_latch", "test", alert=lambda m: None, now=NOW)
        funnel, placed = _execute(engine, monkeypatch)
        assert placed == 0 and _trades(engine) == 0
        assert funnel.halted == 1 and funnel.balances()

    def test_tripping_twice_records_one_halt(self, engine):
        alerts = []
        trip(engine, "stale_inputs", "a", alert=alerts.append, now=NOW)
        trip(engine, "stale_inputs", "b", alert=alerts.append, now=NOW)
        assert len(active_halts(engine)) == 1 and len(alerts) == 1

    def test_clearing_releases_trading(self, engine, monkeypatch):
        trip(engine, "drawdown_latch", "test", alert=lambda m: None, now=NOW)
        assert clear_halts(engine, by="operator", now=NOW) == 1
        _, placed = _execute(engine, monkeypatch)
        assert placed == 1


class TestClearOnlyByToken:
    def test_dry_run_lists_and_clears_nothing(self, engine, monkeypatch, capsys):
        from tests.test_maintenance_entrypoint import run_module

        trip(engine, "bankroll_drop", "test", alert=lambda m: None, now=NOW)
        code, out = run_module(["--clear-halt"], engine, monkeypatch, capsys)
        assert code == 0 and "bankroll_drop" in out
        assert len(active_halts(engine)) == 1

    @pytest.mark.parametrize("bad", ["CLEAR", "WIPE-VOID-SHADOW", "PURGE-ORPHAN-MARKETS"])
    def test_any_other_token_clears_nothing(self, engine, monkeypatch, capsys, bad):
        from tests.test_maintenance_entrypoint import run_module

        trip(engine, "bankroll_drop", "test", alert=lambda m: None, now=NOW)
        code, _ = run_module(["--clear-halt", "--confirm", bad], engine, monkeypatch, capsys)
        assert code == 2 and len(active_halts(engine)) == 1

    def test_the_token_clears(self, engine, monkeypatch, capsys):
        from tests.test_maintenance_entrypoint import run_module

        trip(engine, "bankroll_drop", "test", alert=lambda m: None, now=NOW)
        code, _ = run_module(["--clear-halt", "--confirm", "CLEAR-HALT"], engine, monkeypatch, capsys)
        assert code == 0 and active_halts(engine) == []


# --------------------------------------------------------------------------
# Each switch: trips, stays latched, blocks
# --------------------------------------------------------------------------

class TestDrawdownLatch:
    def test_trips_at_twenty_percent_and_stays_after_a_bounce(self, engine, monkeypatch):
        _fresh_snapshot(engine)
        _set_equity(engine, bankroll=79.0, peak=100.0)
        tripped, alerts = _check(engine)
        assert "drawdown_latch" in tripped and alerts

        _set_equity(engine, bankroll=99.0, peak=100.0)           # bounce
        _check(engine)
        assert [h.switch for h in active_halts(engine)] == ["drawdown_latch"]
        assert _execute(engine, monkeypatch)[1] == 0

    def test_nineteen_percent_does_not_trip(self, engine):
        _fresh_snapshot(engine)
        _set_equity(engine, bankroll=81.0, peak=100.0)
        assert "drawdown_latch" not in _check(engine)[0]


class TestBankrollDrop:
    def test_ten_percent_below_the_24h_high_trips(self, engine, monkeypatch):
        from src.cycle_log import record_cycle

        _fresh_snapshot(engine)
        record_cycle(engine, NOW - dt.timedelta(hours=6), NOW - dt.timedelta(hours=6), True,
                     "market-session", equity=100.0)
        _set_equity(engine, bankroll=89.0, peak=89.5)              # not a 20% drawdown
        tripped, _ = _check(engine)
        assert tripped == ["bankroll_drop"]
        assert _execute(engine, monkeypatch)[1] == 0

    def test_an_old_high_outside_24h_does_not_count(self, engine):
        from src.cycle_log import record_cycle

        _fresh_snapshot(engine)
        record_cycle(engine, NOW - dt.timedelta(hours=30), NOW - dt.timedelta(hours=30), True,
                     "market-session", equity=100.0)
        _set_equity(engine, bankroll=89.0, peak=89.5)
        assert "bankroll_drop" not in _check(engine)[0]


class TestStaleInputs:
    def test_no_fresh_price_in_an_hour_trips(self, engine, monkeypatch):
        _fresh_snapshot(engine, at=NOW - dt.timedelta(minutes=61))
        tripped, _ = _check(engine)
        assert "stale_inputs" in tripped
        _fresh_snapshot(engine, at=NOW)                            # data comes back
        assert active_halts(engine) and _execute(engine, monkeypatch)[1] == 0

    def test_fresh_prices_do_not_trip(self, engine):
        _fresh_snapshot(engine, at=NOW - dt.timedelta(minutes=10))
        assert "stale_inputs" not in _check(engine)[0]


class TestRepeatedErrors:
    def test_three_failed_cycles_in_a_row_latch_through_the_real_path(self, engine, monkeypatch):
        """Through run_pipeline: a cycle that crashes every time never reaches
        the trading stage, so the check runs where the failure is recorded."""
        import contextlib

        import src.run_trading as rt

        @contextlib.contextmanager
        def lock(e):
            yield True

        def crash(*a):
            raise RuntimeError("Kalshi API 503")

        monkeypatch.setattr(rt, "cycle_lock", lock)
        monkeypatch.setattr(rt, "require_production_database", lambda url: None)
        monkeypatch.setattr(rt, "get_engine", lambda url: engine)
        monkeypatch.setattr(rt, "_run_pipeline_locked", crash)
        monkeypatch.setattr(rt, "ping_deadman", lambda url: False)
        monkeypatch.setattr(halts, "_send_alert", lambda msg: None)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                rt.run_pipeline()
        assert [h.switch for h in active_halts(engine)] == ["repeated_errors"]

    def test_two_failures_then_a_success_do_not_trip(self, engine):
        from src.cycle_log import record_cycle

        for i, ok in enumerate((False, False, True)):
            t = NOW - dt.timedelta(minutes=45 - 15 * i)
            record_cycle(engine, t, t, ok, "market-session")
        assert halts.consecutive_failures(engine) == 0


class TestImplausibleEdge:
    def test_a_forty_point_gap_refuses_the_trade_and_latches(self, engine, monkeypatch):
        opp = dict(_qualifying()[0], p_model=0.95, yes_ask=46, evaluated_price=46)
        funnel, placed = _execute(engine, monkeypatch, [opp])
        assert placed == 0
        assert [h.switch for h in active_halts(engine)] == ["implausible_edge"]
        assert _execute(engine, monkeypatch)[1] == 0               # the next one too

    def test_a_no_call_is_measured_on_the_no_side(self, engine, monkeypatch):
        """p_model 0.10 with NO at 56c: NO is worth 90c, a 34-point gap."""
        opp = dict(_qualifying()[0], p_model=0.10, recommended_side="no",
                   evaluated_price=56, yes_bid=44, yes_ask=46)
        assert halts.implausible_edge(opp) is None

    def test_an_ordinary_edge_passes(self, engine):
        assert halts.implausible_edge(dict(_qualifying()[0], evaluated_price=46)) is None


class TestFillSlippage:
    def test_three_cents_trips(self):
        assert halts.fill_slippage(evaluated=46, filled=49) is not None
        assert halts.fill_slippage(evaluated=46, filled=48) is None

    def test_a_slipped_fill_latches_through_execution(self, engine, monkeypatch):
        import src.run_trading as rt
        from src.trading.engine import TradeEngine

        real = TradeEngine.execute

        def slipped(self, **kw):
            result = real(self, **kw)
            if result:
                result = dict(result, price=result["price"] + 4)    # a live fill elsewhere
            return result

        monkeypatch.setattr(TradeEngine, "execute", slipped)
        _execute(engine, monkeypatch, [dict(_qualifying()[0], evaluated_price=46)])
        assert [h.switch for h in active_halts(engine)] == ["fill_slippage"]


def test_thresholds_are_pinned():
    """Loosening a switch is a decision, not an edit."""
    assert halts.DRAWDOWN_LATCH_PCT == 0.20
    assert halts.BANKROLL_DROP_PCT == 0.10
    assert halts.IMPLAUSIBLE_EDGE_POINTS == 40
    assert halts.SLIPPAGE_CENTS == 3
    assert halts.REPEATED_ERROR_CYCLES == 3
    assert halts.STALE_INPUT_MINUTES == 60
