"""Fee-accurate settlement on BOTH execution paths (Phase 1.1, 2026-08-11).

The bug: `PortfolioTracker.close_position` charged a *simulated* Kalshi entry fee
to paper trades and **nothing at all** to live trades:

    fee = kalshi_fee(pos.quantity, pos.entry_price) if is_paper else 0.0

Live fees are real — they are paid on Kalshi at entry — but they never entered the
DB, so live `Trade.realized_pnl` overstated by up to $0.02/contract. That number
feeds `src/portfolio/metrics.py` (win rate, calibration error), the equity curve,
and the Kelly shrinkage multiplier in `src/risk/kelly.py`, which sizes real money.

The fix records the fee at fill time on both paths — simulated for paper, the
actual `KalshiFill.fee` for live — into `Trade.entry_fee`, then settles both with
one formula. PnL becomes path-independent; only the *timing* of the cash movement
stays path-dependent (paper bankroll is equity-at-cost and moves once at
settlement; live bankroll is real cash and moves at fill and again at payout).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.database import Base, ensure_schema, get_session
from src.kalshi.schemas import CreateOrderResponse, KalshiFill
from src.models.market import Market
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.risk.manager import TradeDecision
from src.portfolio.tracker import PortfolioTracker
from src.trading.engine import TradeEngine
from src.trading.fees import kalshi_fee


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _seed(engine, bankroll: float = 100.0, mode: str = "paper"):
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


def _seed_position(
    engine, market_id: str, entry_price: int, qty: int,
    side: str = "no", is_paper: bool = True, entry_fee=None,
):
    """Seed a filled trade + open position, mimicking what the engine writes."""
    with get_session(engine) as s:
        s.add(Position(
            market_id=market_id, side=side, entry_price=entry_price,
            quantity=qty, current_price=entry_price, status="open",
        ))
        trade = Trade(
            market_id=market_id, side=side, action="buy", price=entry_price,
            quantity=qty, p_model=0.5, implied_prob=0.5, edge=0.0, net_ev=0.0,
            position_size_dollars=entry_price * qty / 100.0, confidence=0.8,
            reasoning="seed", is_paper=is_paper, status="filled",
        )
        trade.entry_fee = entry_fee
        s.add(trade)
        s.commit()


def _decision(side="no", price=50, qty=10):
    return TradeDecision(
        approved=True, side=side, position_size_dollars=price * qty / 100.0,
        quantity=qty, price_cents=price, rejection_reasons=[],
    )


def _live_client(order_id="ord-1", fee_cents=None, fills_raise=False):
    client = AsyncMock()
    client.place_order.return_value = CreateOrderResponse(
        order_id=order_id, ticker="TEST-MKT", status="resting",
    )
    client.get_order.return_value = {
        "order": {"order_id": order_id, "status": "executed", "remaining_count": 0}
    }
    if fills_raise:
        client.get_fills.side_effect = RuntimeError("fills endpoint down")
    else:
        client.get_fills.return_value = [
            KalshiFill(
                fill_id="f1", order_id=order_id, ticker="TEST-MKT", side="no",
                count=10, yes_price=50, no_price=50, fee=fee_cents or 0,
            )
        ]
    return client


class _TradeSnapshot:
    """Detached copy of a Trade row — ORM instances expire once the session closes."""

    def __init__(self, row: Trade):
        self.price = row.price
        self.quantity = row.quantity
        self.entry_fee = row.entry_fee
        self.entry_fee_source = row.entry_fee_source
        self.status = row.status
        self.is_paper = row.is_paper


def _trade(engine, market_id="TEST-MKT") -> _TradeSnapshot:
    with get_session(engine) as s:
        row = (
            s.query(Trade).filter_by(market_id=market_id)
            .order_by(Trade.id.desc()).first()
        )
        assert row is not None, f"no trade recorded for {market_id}"
        return _TradeSnapshot(row)


# --------------------------------------------------------------------------
# 1. The headline assertion: paper and live PnL math agree
# --------------------------------------------------------------------------

class TestPaperLiveSymmetry:
    def test_identical_trades_settle_to_identical_pnl(self, db_engine, tmp_path):
        """Same side/price/qty/outcome + same fee => byte-identical realized PnL.

        This is the regression that proves the `is_paper` branch is gone from the
        PnL formula. Before the fix the live trade settled to $5.00 and the paper
        trade to $4.82 for the exact same economics.
        """
        from src.database import get_engine

        fee = kalshi_fee(10, 50)

        paper_engine = db_engine
        _seed(paper_engine)
        _seed_position(paper_engine, "TEST-MKT", 50, 10, is_paper=True, entry_fee=fee)
        paper = PortfolioTracker(paper_engine).close_position("TEST-MKT", exit_price=0)

        live_engine = get_engine(f"sqlite:///{tmp_path}/live.db")
        _seed(live_engine, mode="live")
        _seed_position(live_engine, "TEST-MKT", 50, 10, is_paper=False, entry_fee=fee)
        live = PortfolioTracker(live_engine).close_position("TEST-MKT", exit_price=0)

        assert paper["realized_pnl"] == live["realized_pnl"]
        assert paper["fee"] == live["fee"] == fee

    def test_live_no_longer_settles_at_gross(self, db_engine):
        """The bug itself: live PnL must not be the fee-free gross number."""
        _seed(db_engine, mode="live")
        fee = kalshi_fee(10, 50)
        _seed_position(db_engine, "TEST-MKT", 50, 10, is_paper=False, entry_fee=fee)
        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=0)
        # gross = (100 - 50) * 10 / 100 = $5.00
        assert closed["realized_pnl"] == pytest.approx(5.00 - fee)
        assert closed["realized_pnl"] < 5.00


# --------------------------------------------------------------------------
# 2. Settlement consumes the recorded fee, whatever its size
# --------------------------------------------------------------------------

class TestRecordedFeeIsAuthoritative:
    def test_settles_net_of_actual_kalshi_fee(self, db_engine):
        """A real Kalshi fee of 23c settles to gross - $0.23, not the simulation."""
        _seed(db_engine, mode="live")
        _seed_position(db_engine, "TEST-MKT", 50, 10, is_paper=False, entry_fee=0.23)
        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=0)
        assert closed["fee"] == 0.23
        assert closed["realized_pnl"] == pytest.approx(5.00 - 0.23)

    def test_zero_recorded_fee_is_honoured(self, db_engine):
        """A genuinely fee-free fill (maker rebate/edge price) records 0.0 and is
        NOT overwritten by the simulated estimate — 0.0 differs from None."""
        _seed(db_engine, mode="live")
        _seed_position(db_engine, "TEST-MKT", 50, 10, is_paper=False, entry_fee=0.0)
        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=0)
        assert closed["fee"] == 0.0
        assert closed["realized_pnl"] == pytest.approx(5.00)


# --------------------------------------------------------------------------
# 3. Legacy rows keep today's behaviour
# --------------------------------------------------------------------------

class TestLegacyRows:
    def test_legacy_paper_row_unchanged(self, db_engine):
        """Rows written before this change have entry_fee=None and must settle
        exactly as they do on main — no silent rewrite of history."""
        _seed(db_engine)
        _seed_position(db_engine, "TEST-MKT", 50, 10, is_paper=True, entry_fee=None)
        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=0)
        assert closed["fee"] == kalshi_fee(10, 50)
        assert closed["realized_pnl"] == pytest.approx(5.00 - kalshi_fee(10, 50))

    def test_legacy_live_row_estimates_never_zero(self, db_engine):
        """A legacy live row has no recorded fee, but 0.0 is known-wrong: the fee
        was really paid on Kalshi. Fall back to the simulated estimate."""
        _seed(db_engine, mode="live")
        _seed_position(db_engine, "TEST-MKT", 50, 10, is_paper=False, entry_fee=None)
        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=0)
        assert closed["fee"] == kalshi_fee(10, 50)
        assert closed["fee"] > 0.0


# --------------------------------------------------------------------------
# 4. Fills record a fee at execution time
# --------------------------------------------------------------------------

class TestFeeRecordedAtFill:
    def test_paper_fill_records_simulated_fee(self, db_engine):
        _seed(db_engine)
        TradeEngine(db_engine).execute(
            _decision(), "TEST-MKT", p_model=0.6, implied_prob=0.5, edge=0.1,
            net_ev=0.05, confidence=0.8, reasoning="t", yes_bid=48, yes_ask=52,
        )
        t = _trade(db_engine)
        assert t.entry_fee == kalshi_fee(t.quantity, t.price)
        assert t.entry_fee_source == "simulated"

    def test_live_fill_records_real_fee_from_fills_api(self, db_engine):
        _seed(db_engine, mode="live")
        client = _live_client(fee_cents=17)
        TradeEngine(db_engine, kalshi_client=client).execute(
            _decision(), "TEST-MKT", p_model=0.6, implied_prob=0.5, edge=0.1,
            net_ev=0.05, confidence=0.8, reasoning="t", yes_bid=48, yes_ask=52,
        )
        t = _trade(db_engine)
        assert t.entry_fee == pytest.approx(0.17)  # 17 cents -> dollars
        assert t.entry_fee_source == "kalshi_fills"

    def test_live_fill_falls_back_to_estimate_when_fills_unavailable(self, db_engine):
        """Fills endpoint down must never yield a 0.0 fee — that is the bug we are
        fixing. Estimate and mark it as an estimate."""
        _seed(db_engine, mode="live")
        client = _live_client(fills_raise=True)
        TradeEngine(db_engine, kalshi_client=client).execute(
            _decision(), "TEST-MKT", p_model=0.6, implied_prob=0.5, edge=0.1,
            net_ev=0.05, confidence=0.8, reasoning="t", yes_bid=48, yes_ask=52,
        )
        t = _trade(db_engine)
        assert t.entry_fee == kalshi_fee(t.quantity, t.price)
        assert t.entry_fee_source == "estimated"
        assert t.entry_fee > 0.0


# --------------------------------------------------------------------------
# 5. Cash ledger: no double-debit, net change identical across paths
# --------------------------------------------------------------------------

class TestCashLedger:
    def test_live_round_trip_nets_gross_minus_fee(self, db_engine):
        """A live round trip must move the bankroll by exactly gross - fee.

        REVISED 2026-08-11 (Phase 1.5). The Phase 1 version asserted the live
        path debited cost+fee at fill and credited the payout at settlement —
        correct arithmetic, but it left live on a cash ledger while paper stayed
        on equity-at-cost, so the two paths' bankrolls (which every risk limit
        divides by) meant different things. Both now use the equity-at-cost
        ledger: untouched at fill, moved once at settlement. The round-trip
        total is unchanged, which is the point.
        """
        _seed(db_engine, bankroll=100.0, mode="live")
        client = _live_client(fee_cents=17)
        TradeEngine(db_engine, kalshi_client=client).execute(
            _decision(side="no", price=50, qty=10), "TEST-MKT",
            p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
            confidence=0.8, reasoning="t", yes_bid=48, yes_ask=52,
        )
        t = _trade(db_engine)
        with get_session(db_engine) as s:
            after_fill = s.query(TradingSettings).first().bankroll
        assert after_fill == pytest.approx(100.0)  # cash swapped for position

        # NO position, market resolves YES -> side_exit = 0 -> total loss.
        PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=100)
        with get_session(db_engine) as s:
            after_settle = s.query(TradingSettings).first().bankroll
        gross = (0 - t.price) * t.quantity / 100.0
        assert after_settle == pytest.approx(100.0 + gross - 0.17)

    def test_paper_bankroll_moves_only_at_settlement(self, db_engine):
        """Paper bankroll is equity-at-cost: unchanged at fill, moves by realized
        PnL at settlement. Unchanged behaviour — asserted so the live fix cannot
        drift the paper convention."""
        _seed(db_engine, bankroll=100.0)
        TradeEngine(db_engine).execute(
            _decision(side="no", price=50, qty=10), "TEST-MKT",
            p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
            confidence=0.8, reasoning="t", yes_bid=48, yes_ask=52,
        )
        with get_session(db_engine) as s:
            assert s.query(TradingSettings).first().bankroll == 100.0

        closed = PortfolioTracker(db_engine).close_position("TEST-MKT", exit_price=100)
        with get_session(db_engine) as s:
            after = s.query(TradingSettings).first().bankroll
        assert after == pytest.approx(100.0 + closed["realized_pnl"])


# --------------------------------------------------------------------------
# 6. Additive schema migration (no Alembic in this repo)
# --------------------------------------------------------------------------

class TestEnsureSchema:
    def _legacy_trades_table(self, engine):
        """Build the trades table as it existed before entry_fee, with a row."""
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS trades"))
            conn.execute(text(
                "CREATE TABLE trades ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " market_id VARCHAR(100), side VARCHAR(10), action VARCHAR(10),"
                " price INTEGER, quantity INTEGER, p_model FLOAT,"
                " implied_prob FLOAT, edge FLOAT, net_ev FLOAT,"
                " position_size_dollars FLOAT, confidence FLOAT, reasoning TEXT,"
                " is_paper BOOLEAN, status VARCHAR(20), order_id VARCHAR(100),"
                " exit_price INTEGER, realized_pnl FLOAT, created_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO trades (market_id, side, action, price, quantity,"
                " p_model, implied_prob, edge, net_ev, position_size_dollars,"
                " confidence, reasoning, is_paper, status)"
                " VALUES ('OLD-1','no','buy',50,10,0.5,0.5,0.0,0.0,5.0,0.8,'x',1,'filled')"
            ))

    def test_adds_missing_column_and_preserves_rows(self, db_engine):
        self._legacy_trades_table(db_engine)
        ensure_schema(db_engine)
        with get_session(db_engine) as s:
            row = s.query(Trade).filter_by(market_id="OLD-1").first()
            assert row is not None            # data survived
            assert row.entry_fee is None      # new column, null for legacy rows
            assert row.quantity == 10

    def test_is_idempotent(self, db_engine):
        self._legacy_trades_table(db_engine)
        ensure_schema(db_engine)
        ensure_schema(db_engine)
        ensure_schema(db_engine)
        with get_session(db_engine) as s:
            assert s.query(Trade).filter_by(market_id="OLD-1").count() == 1

    def test_no_op_on_current_schema(self, db_engine):
        Base.metadata.create_all(db_engine)
        ensure_schema(db_engine)
        _seed(db_engine)
        _seed_position(db_engine, "TEST-MKT", 50, 10, entry_fee=0.18)
        assert _trade(db_engine).entry_fee == 0.18
