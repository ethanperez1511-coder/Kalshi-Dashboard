"""One bankroll definition for paper and live (Phase 1.5, 2026-08-11).

Before this, the two paths measured different things:

  paper — `bankroll` moved only at settlement, so it was equity-at-cost:
          cash plus the cost basis of everything still open.
  live  — `_mark_trade_filled` debited the entry cost, and `sync_live_bankroll`
          overwrote the field with Kalshi's **cash** balance, which excludes
          open positions entirely.

Every risk limit divides exposure by that field. So in live the denominator
shrank as positions opened while the numerator grew, and the 25% exposure cap
bound far earlier than it does in paper. Fail-safe, but it meant the 50-trade
paper evaluation was measuring different sizing behaviour than live would use —
which defeats the point of the paper window.

Both paths now keep the same equity-at-cost ledger, and every limit and the
Kelly sizer read one number: `total_equity` = cash + mark-to-market value of
open positions.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.database import Base, get_engine, get_session
from src.models.market import Market
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.portfolio.equity import total_equity
from src.portfolio.tracker import PortfolioTracker
from src.risk.limits import LimitsChecker
from src.risk.manager import RiskManager
from src.ev.calculator import calculate_ev
from src.risk.manager import TradeDecision  # noqa: F401  (kept for symmetry)
from src.trading.engine import TradeEngine, sync_live_bankroll
from src.trading.fees import kalshi_fee


def _seed(engine, bankroll=100.0, mode="paper"):
    Base.metadata.create_all(engine)
    with get_session(engine) as s:
        st = TradingSettings()
        st.mode = mode
        st.bankroll = bankroll
        st.peak_bankroll = bankroll
        st.paper_trade_count = 55 if mode == "live" else 0
        st.paper_trades_before_live = 50
        s.add(st)
        s.add(Market(
            market_id="TEST-MKT", title="Test", category="General",
            close_date=datetime(2026, 12, 31, tzinfo=timezone.utc), status="open",
        ))
        s.commit()


def _open_position(engine, entry=50, qty=10, current=None, side="no", is_paper=True):
    with get_session(engine) as s:
        s.add(Position(
            market_id="TEST-MKT", side=side, entry_price=entry, quantity=qty,
            current_price=entry if current is None else current, status="open",
        ))
        t = Trade(
            market_id="TEST-MKT", side=side, action="buy", price=entry, quantity=qty,
            p_model=0.5, implied_prob=0.5, edge=0.0, net_ev=0.0,
            position_size_dollars=entry * qty / 100.0, confidence=0.8,
            reasoning="seed", is_paper=is_paper, status="filled",
        )
        t.entry_fee = kalshi_fee(qty, entry)
        s.add(t)
        s.commit()


# --------------------------------------------------------------------------
# 1. The definition itself
# --------------------------------------------------------------------------

class TestTotalEquity:
    def test_equity_is_cash_plus_mark_to_market(self, db_engine):
        _seed(db_engine, bankroll=100.0)
        # Bought NO @50c x10 (cost $5.00), now marked at 70c => +$2.00 unrealised.
        _open_position(db_engine, entry=50, qty=10, current=70)
        assert total_equity(db_engine) == pytest.approx(102.00)

    def test_equity_falls_when_a_position_is_marked_down(self, db_engine):
        _seed(db_engine, bankroll=100.0)
        _open_position(db_engine, entry=50, qty=10, current=20)
        assert total_equity(db_engine) == pytest.approx(97.00)

    def test_equity_equals_bankroll_with_no_open_positions(self, db_engine):
        _seed(db_engine, bankroll=101.35)
        assert total_equity(db_engine) == pytest.approx(101.35)


# --------------------------------------------------------------------------
# 2. THE requirement: paper and live must agree, exactly
# --------------------------------------------------------------------------

class TestPaperLiveAgreement:
    def _build(self, tmp_path, name, mode):
        engine = get_engine(f"sqlite:///{tmp_path}/{name}.db")
        _seed(engine, bankroll=100.0, mode=mode)
        _open_position(engine, entry=50, qty=10, current=70, is_paper=(mode == "paper"))
        return engine

    def test_identical_state_yields_identical_equity(self, tmp_path):
        paper = self._build(tmp_path, "paper", "paper")
        live = self._build(tmp_path, "live", "live")
        assert total_equity(paper) == total_equity(live)

    def test_identical_state_yields_identical_limit_decisions(self, tmp_path):
        """The number the limits actually divide by must be the same number."""
        paper = self._build(tmp_path, "paper2", "paper")
        live = self._build(tmp_path, "live2", "live")

        # Equity is $102.00, so the 3% single-trade cap is $3.06. $5.00 breaches
        # it; both paths must reject, with the identical message — same number,
        # same arithmetic.
        p = LimitsChecker(paper).check(5.00, "TEST-MKT", "General")
        l = LimitsChecker(live).check(5.00, "TEST-MKT", "General")
        assert p.approved is False and l.approved is False
        assert p.violations == l.violations
        assert "3.06" in p.violations[0]

        # And a size inside every limit must be approved on both.
        p_ok = LimitsChecker(paper).check(0.50, "OTHER-MKT", "General")
        l_ok = LimitsChecker(live).check(0.50, "OTHER-MKT", "General")
        assert p_ok.approved == l_ok.approved is True

    def test_identical_state_yields_identical_position_sizing(self, tmp_path):
        paper = self._build(tmp_path, "paper3", "paper")
        live = self._build(tmp_path, "live3", "live")
        ev = calculate_ev(p_model=0.60, price_cents=50, yes_bid=49, yes_ask=51)

        dp = RiskManager(paper).evaluate(ev, 0.8, "NEW-MKT", "General")
        dl = RiskManager(live).evaluate(ev, 0.8, "NEW-MKT", "General")
        assert dp.position_size_dollars == dl.position_size_dollars
        assert dp.quantity == dl.quantity


# --------------------------------------------------------------------------
# 3. Ledger symmetry: neither path moves the bankroll at fill
# --------------------------------------------------------------------------

class TestLedgerSymmetry:
    def _decision(self):
        from src.risk.manager import TradeDecision
        return TradeDecision(
            approved=True, side="no", position_size_dollars=5.0,
            quantity=10, price_cents=50, rejection_reasons=[],
        )

    def _execute(self, engine, client=None):
        TradeEngine(engine, kalshi_client=client).execute(
            self._decision(), "TEST-MKT", p_model=0.6, implied_prob=0.5,
            edge=0.1, net_ev=0.05, confidence=0.8, reasoning="t",
            yes_bid=48, yes_ask=52,
        )

    def _bankroll(self, engine):
        with get_session(engine) as s:
            return s.query(TradingSettings).first().bankroll

    def test_paper_fill_does_not_move_bankroll(self, db_engine):
        _seed(db_engine, bankroll=100.0)
        self._execute(db_engine)
        assert self._bankroll(db_engine) == 100.0

    def test_live_fill_does_not_move_bankroll_either(self, db_engine):
        """Previously the live path debited the entry cost here, which is what
        made the two ledgers incomparable."""
        from unittest.mock import AsyncMock
        from src.kalshi.schemas import CreateOrderResponse, KalshiFill

        _seed(db_engine, bankroll=100.0, mode="live")
        client = AsyncMock()
        client.place_order.return_value = CreateOrderResponse(
            order_id="o1", ticker="TEST-MKT", status="resting",
        )
        client.get_order.return_value = {
            "order": {"order_id": "o1", "status": "executed", "remaining_count": 0}
        }
        client.get_fills.return_value = [KalshiFill(
            fill_id="f", order_id="o1", ticker="TEST-MKT", side="no",
            count=10, yes_price=50, no_price=50, fee=17,
        )]
        self._execute(db_engine, client)
        assert self._bankroll(db_engine) == 100.0

    def test_settlement_moves_bankroll_by_realized_pnl_on_both_paths(self, tmp_path):
        for name, mode, is_paper in (("p", "paper", True), ("l", "live", False)):
            engine = get_engine(f"sqlite:///{tmp_path}/settle_{name}.db")
            _seed(engine, bankroll=100.0, mode=mode)
            _open_position(engine, entry=50, qty=10, is_paper=is_paper)
            closed = PortfolioTracker(engine).close_position("TEST-MKT", exit_price=0)
            with get_session(engine) as s:
                assert s.query(TradingSettings).first().bankroll == pytest.approx(
                    100.0 + closed["realized_pnl"], abs=0.005
                )


# --------------------------------------------------------------------------
# 4. Live sync must produce equity-at-cost, not raw cash
# --------------------------------------------------------------------------

class TestLiveSync:
    def test_sync_adds_open_cost_basis_to_kalshi_cash(self, db_engine):
        """Kalshi reports cash only. Storing that raw is what made live's
        bankroll mean something different from paper's."""
        from unittest.mock import AsyncMock
        from src.kalshi.schemas import KalshiBalance

        _seed(db_engine, bankroll=0.0, mode="live")
        _open_position(db_engine, entry=50, qty=10, is_paper=False)  # $5.00 at cost

        client = AsyncMock()
        client.get_balance.return_value = KalshiBalance(balance=9500)  # $95.00 cash
        synced = sync_live_bankroll(db_engine, client)

        assert synced == pytest.approx(100.00)  # 95 cash + 5 cost basis
        assert self_bankroll(db_engine) == pytest.approx(100.00)


def self_bankroll(engine):
    with get_session(engine) as s:
        return s.query(TradingSettings).first().bankroll
