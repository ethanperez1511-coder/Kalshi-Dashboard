"""Tests for velocity (expiry filter, held-market dedup), fee-accurate
settlement, and odds-quota conservation.

Each change either tightens the trade set or makes paper PnL more conservative;
none loosen a risk limit or touch the live gate.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.database import get_engine, Base, get_session
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import TradeDecision
from src.trading.engine import TradeEngine
from src.trading.fees import kalshi_fee


@pytest.fixture
def engine():
    eng = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with get_session(eng) as s:
        s.add(TradingSettings(bankroll=100.0, peak_bankroll=100.0, mode="paper",
                              paper_trade_count=0, paper_trades_before_live=50))
        s.commit()
    return eng


# ---------- Fee math ----------

class TestKalshiFee:
    def test_fee_zero_at_extremes(self):
        assert kalshi_fee(10, 0) == 0.0
        assert kalshi_fee(10, 100) == 0.0

    def test_fee_largest_near_midpoint(self):
        mid = kalshi_fee(100, 50)
        edge = kalshi_fee(100, 90)
        assert mid > edge > 0

    def test_fee_matches_formula_rounds_up(self):
        # 0.07 * 3 * 0.85 * 0.15 = 0.0268 -> ceil to 0.03
        assert kalshi_fee(3, 85) == 0.03

    def test_fee_scales_with_quantity(self):
        assert kalshi_fee(200, 50) > kalshi_fee(100, 50)

    def test_zero_quantity_no_fee(self):
        assert kalshi_fee(0, 50) == 0.0


# ---------- Fee-accurate settlement ----------

class TestFeeAccurateSettlement:
    def _seed_paper_position(self, engine, entry_price, qty, side="no"):
        with get_session(engine) as s:
            s.add(Position(market_id="M1", side=side, entry_price=entry_price,
                           quantity=qty, current_price=entry_price, status="open"))
            s.add(Trade(market_id="M1", side=side, action="buy", price=entry_price,
                        quantity=qty, p_model=0.5, implied_prob=0.5, edge=0.0,
                        net_ev=0.0, position_size_dollars=entry_price * qty / 100.0,
                        confidence=0.8, reasoning="t", is_paper=True, status="filled"))
            s.commit()

    def test_paper_win_pnl_net_of_fee(self, engine):
        # NO @ 50c x10, resolves NO (win): gross = (100-... ) wait use side math.
        # side_exit for NO win = 100 - 0 = 100; entry 50 -> gross (100-50)*10/100 = $5.00
        # fee = kalshi_fee(10, 50) = ceil(0.07*10*0.5*0.5*100)/100 = ceil(17.5)/100=0.18
        self._seed_paper_position(engine, entry_price=50, qty=10, side="no")
        tracker = PortfolioTracker(engine)
        result = tracker.close_position("M1", exit_price=0, finalize_market=True)
        expected_fee = kalshi_fee(10, 50)
        assert abs(result["realized_pnl"] - (5.00 - expected_fee)) < 1e-9
        assert result["fee"] == expected_fee

    def test_bankroll_reflects_fee(self, engine):
        self._seed_paper_position(engine, entry_price=50, qty=10, side="no")
        tracker = PortfolioTracker(engine)
        tracker.close_position("M1", exit_price=0)
        with get_session(engine) as s:
            bankroll = s.query(TradingSettings).first().bankroll
        # 100 + 5.00 - fee
        assert abs(bankroll - (100.0 + 5.00 - kalshi_fee(10, 50))) < 1e-9

    def _seed_live_position(self, engine, entry_fee=None):
        with get_session(engine) as s:
            s.add(Position(market_id="ML", side="no", entry_price=50, quantity=10,
                           current_price=50, status="open"))
            trade = Trade(market_id="ML", side="no", action="buy", price=50, quantity=10,
                          p_model=0.5, implied_prob=0.5, edge=0.0, net_ev=0.0,
                          position_size_dollars=5.0, confidence=0.8, reasoning="t",
                          is_paper=False, status="filled")
            trade.entry_fee = entry_fee
            s.add(trade)
            s.commit()

    def test_live_position_settles_net_of_recorded_fee(self, engine):
        """SUPERSEDES test_live_position_no_simulated_fee (2026-08-11).

        The old test asserted live PnL was the fee-free gross number, on the
        reasoning that Kalshi already charged the fee so charging it again would
        double-count. The cash reasoning was right; the PnL conclusion was wrong.
        Kalshi takes the fee at entry, so it never reaches the DB — leaving it
        out of `realized_pnl` made every live trade look better than it was, and
        that number feeds the Kelly shrinkage multiplier. The fee is now recorded
        at fill time and consumed here. Double-charging is prevented in the cash
        ledger instead (see TestCashLedger in tests/test_settlement_fees.py).
        """
        self._seed_live_position(engine, entry_fee=0.18)
        result = PortfolioTracker(engine).close_position("ML", exit_price=0)
        assert result["fee"] == 0.18
        assert abs(result["realized_pnl"] - (5.00 - 0.18)) < 1e-9

    def test_live_position_without_recorded_fee_estimates(self, engine):
        # Legacy row predating entry_fee: 0.0 is known-wrong, so estimate.
        self._seed_live_position(engine, entry_fee=None)
        result = PortfolioTracker(engine).close_position("ML", exit_price=0)
        assert result["fee"] == kalshi_fee(10, 50)
        assert abs(result["realized_pnl"] - (5.00 - kalshi_fee(10, 50))) < 1e-9


# ---------- Held-market dedup ----------

class TestSkipHeldMarkets:
    def _decision(self):
        return TradeDecision(
            approved=True, side="no", quantity=3, price_cents=85,
            position_size_dollars=2.55, rejection_reasons=[],
        )

    def test_second_entry_same_market_skipped(self, engine):
        eng = engine
        te = TradeEngine(eng)
        kw = dict(market_id="M1", p_model=0.6, implied_prob=0.55, edge=0.05,
                  net_ev=0.05, confidence=0.8, reasoning="t", yes_bid=14, yes_ask=16)
        first = te.execute(decision=self._decision(), **kw)
        second = te.execute(decision=self._decision(), **kw)
        assert first is not None
        assert second is None  # already held -> skipped
        with get_session(eng) as s:
            positions = s.query(Position).filter_by(market_id="M1", status="open").all()
            assert len(positions) == 1
            assert positions[0].quantity == 3  # not doubled

    def test_paper_count_not_wasted_on_dup(self, engine):
        eng = engine
        te = TradeEngine(eng)
        kw = dict(market_id="M1", p_model=0.6, implied_prob=0.55, edge=0.05,
                  net_ev=0.05, confidence=0.8, reasoning="t", yes_bid=14, yes_ask=16)
        te.execute(decision=self._decision(), **kw)
        te.execute(decision=self._decision(), **kw)
        with get_session(eng) as s:
            assert s.query(TradingSettings).first().paper_trade_count == 1


# ---------- Expiry filter (filter-level) ----------

class TestExpiryFilter:
    def test_too_far_out_rejected(self):
        from src.ev.filter import TradeFilter
        from src.ev.calculator import calculate_ev
        f = TradeFilter(max_hours_to_expiry=14 * 24)
        ev = calculate_ev(p_model=0.6, price_cents=55, yes_bid=54, yes_ask=56)
        res = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=500,
                         bid_ask_spread_cents=2, hours_to_expiry=30 * 24)
        assert res.status == "rejected"
        assert any("expiry" in r.lower() or "far" in r.lower() for r in res.rejection_reasons)

    def test_within_window_allowed(self):
        from src.ev.filter import TradeFilter
        from src.ev.calculator import calculate_ev
        f = TradeFilter(max_hours_to_expiry=14 * 24)
        ev = calculate_ev(p_model=0.6, price_cents=55, yes_bid=54, yes_ask=56)
        res = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=500,
                         bid_ask_spread_cents=2, hours_to_expiry=5 * 24)
        # not rejected for expiry reason
        assert not any("far" in r.lower() for r in res.rejection_reasons)

    def test_zero_disables_filter(self):
        from src.ev.filter import TradeFilter
        from src.ev.calculator import calculate_ev
        f = TradeFilter(max_hours_to_expiry=0)
        ev = calculate_ev(p_model=0.6, price_cents=55, yes_bid=54, yes_ask=56)
        res = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=500,
                         bid_ask_spread_cents=2, hours_to_expiry=999 * 24)
        assert not any("far" in r.lower() for r in res.rejection_reasons)


# ---------- Odds quota conservation ----------

class TestOddsCache:
    def test_cross_instance_ttl_cache(self):
        from src.modeling import odds_api

        odds_api._clear_module_cache()
        calls = {"n": 0}

        class _FakeHTTP:
            @staticmethod
            def get(url, params=None, timeout=None):
                calls["n"] += 1
                class _R:
                    status_code = 200
                    def json(self_inner):
                        return []
                    def raise_for_status(self_inner):
                        pass
                return _R()

        # Two separate clients (simulating two cycles) share the module cache.
        c1 = odds_api.OddsClient("k", sport_keys=["baseball_mlb"], http=_FakeHTTP())
        c1.get_all_odds()
        n_after_first = calls["n"]
        c2 = odds_api.OddsClient("k", sport_keys=["baseball_mlb"], http=_FakeHTTP())
        c2.get_all_odds()
        assert calls["n"] == n_after_first  # served from cache, no new HTTP call

    def test_quota_dead_flag(self):
        from src.modeling import odds_api

        odds_api._clear_module_cache()

        class _DeadHTTP:
            @staticmethod
            def get(url, params=None, timeout=None):
                class _R:
                    status_code = 401
                    def json(self_inner):
                        return {}
                    def raise_for_status(self_inner):
                        pass
                return _R()

        c = odds_api.OddsClient("k", sport_keys=["baseball_mlb"], http=_DeadHTTP())
        games = c.get_all_odds()
        assert games == []
        assert c.quota_dead is True
