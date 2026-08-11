"""Tests for live trading: client order methods and _execute_live() flow."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.database import get_session, Base
from src.kalshi.client import KalshiClient
from src.kalshi.schemas import CreateOrderResponse, CancelOrderResponse
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.models.position import Position
from src.risk.manager import TradeDecision
from src.trading.engine import TradeEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_live_ready(db_engine):
    """Set up DB with live mode enabled and 50+ paper trades."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.mode = "live"
        settings.paper_trade_count = 55
        settings.paper_trades_before_live = 50
        settings.bankroll = 100.0
        session.add(settings)
        session.add(Market(
            market_id="TEST-MKT", title="Test",
            category="General",
            close_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
            status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="TEST-MKT", yes_bid=50, yes_ask=52, last_price=51,
                volume=1000,
                timestamp=datetime(2026, 5, 1, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()


def _make_decision(side="yes", price=50, qty=2, dollars=1.0):
    return TradeDecision(
        approved=True, side=side,
        position_size_dollars=dollars,
        quantity=qty,
        price_cents=price,
        rejection_reasons=[],
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Client method tests (mock httpx)
# ---------------------------------------------------------------------------

class TestKalshiClientOrders:
    @pytest.fixture
    def mock_client(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        http.base_url = httpx.URL("https://api.kalshi.com/trade-api/v2")
        return KalshiClient(http_client=http)

    def test_place_order(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "order": {
                "order_id": "ord-123",
                "ticker": "TEST",
                "status": "resting",
                "remaining_count": 5,
            }
        }
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.place_order("TEST", "yes", 5, 50))
        assert result.order_id == "ord-123"
        assert result.status == "resting"
        assert result.remaining_count == 5

    def test_cancel_order(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"order_id": "ord-123", "reduced_by": 3}
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.cancel_order("ord-123"))
        assert result.order_id == "ord-123"
        assert result.reduced_by == 3

    def test_get_order(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"order": {"order_id": "ord-123", "status": "executed"}}
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.get_order("ord-123"))
        assert result["order"]["status"] == "executed"

    def test_get_balance(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"balance": 5000, "portfolio_value": 8000}
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.get_balance())
        assert result.balance == 5000
        assert result.portfolio_value == 8000

    def test_get_positions(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "market_positions": [
                {"ticker": "ABC", "position": 10, "total_traded": 20, "realized_pnl": 50}
            ]
        }
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.get_positions())
        assert len(result) == 1
        assert result[0].ticker == "ABC"
        assert result[0].position == 10

    def test_get_fills(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "fills": [
                {"fill_id": "f1", "order_id": "o1", "ticker": "X", "side": "yes",
                 "count": 2, "yes_price": 55, "no_price": 45, "fee": 1}
            ]
        }
        mock_client._http.request = AsyncMock(return_value=resp)

        result = _run(mock_client.get_fills(ticker="X"))
        assert len(result) == 1
        assert result[0].fill_id == "f1"


# ---------------------------------------------------------------------------
# TradeEngine._execute_live() tests
# ---------------------------------------------------------------------------

class TestExecuteLive:
    def test_live_filled(self, db_engine):
        """Order placed, polled, filled → trade status=filled, position created."""
        _setup_live_ready(db_engine)

        mock_client = AsyncMock()
        mock_client.place_order.return_value = CreateOrderResponse(
            order_id="ord-abc", ticker="TEST-MKT", status="resting",
        )
        # First poll: still resting. Second poll: executed.
        mock_client.get_order.side_effect = [
            {"order": {"order_id": "ord-abc", "status": "resting", "remaining_count": 2}},
            {"order": {"order_id": "ord-abc", "status": "executed", "remaining_count": 0}},
        ]

        engine = TradeEngine(db_engine, kalshi_client=mock_client)
        engine._fill_timeout = 15  # short for test
        decision = _make_decision()

        result = engine.execute(
            decision=decision, market_id="TEST-MKT",
            p_model=0.60, implied_prob=0.50, edge=0.10, net_ev=0.05,
            confidence=0.80, reasoning="test live",
        )

        assert result is not None
        assert result["is_paper"] is False
        assert result["status"] == "filled"
        assert result["order_id"] == "ord-abc"

        # Verify DB state
        with get_session(db_engine) as session:
            trade = session.query(Trade).filter_by(order_id="ord-abc").first()
            assert trade is not None
            assert trade.status == "filled"
            assert trade.is_paper is False

            pos = session.query(Position).filter_by(market_id="TEST-MKT").first()
            assert pos is not None
            assert pos.status == "open"

            # SUPERSEDED 2026-08-11 (Phase 1.5): the fill no longer debits the
            # bankroll on either path. It is an equity-at-cost ledger that moves
            # only at settlement; the open position above is what carries the
            # exposure. See tests/test_equity_semantics.py.
            settings = session.query(TradingSettings).first()
            assert settings.bankroll == 100.0

    def test_live_timeout_cancelled(self, db_engine):
        """Order times out → cancelled."""
        _setup_live_ready(db_engine)

        mock_client = AsyncMock()
        mock_client.place_order.return_value = CreateOrderResponse(
            order_id="ord-timeout", ticker="TEST-MKT", status="resting",
        )
        # Always resting
        mock_client.get_order.return_value = {
            "order": {"order_id": "ord-timeout", "status": "resting", "remaining_count": 2}
        }
        mock_client.cancel_order.return_value = CancelOrderResponse(
            order_id="ord-timeout", reduced_by=2,
        )

        engine = TradeEngine(db_engine, kalshi_client=mock_client)
        engine._fill_timeout = 6  # very short timeout

        decision = _make_decision()
        result = engine.execute(
            decision=decision, market_id="TEST-MKT",
            p_model=0.60, implied_prob=0.50, edge=0.10, net_ev=0.05,
            confidence=0.80, reasoning="test timeout",
        )

        assert result["status"] == "cancelled"
        assert result["is_paper"] is False
        mock_client.cancel_order.assert_called_once_with("ord-timeout")

        with get_session(db_engine) as session:
            trade = session.query(Trade).filter_by(order_id="ord-timeout").first()
            assert trade.status == "cancelled"

    def test_live_api_error(self, db_engine):
        """API error during place_order → trade marked error."""
        _setup_live_ready(db_engine)

        mock_client = AsyncMock()
        mock_client.place_order.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(),
        )

        engine = TradeEngine(db_engine, kalshi_client=mock_client)
        decision = _make_decision()

        result = engine.execute(
            decision=decision, market_id="TEST-MKT",
            p_model=0.60, implied_prob=0.50, edge=0.10, net_ev=0.05,
            confidence=0.80, reasoning="test error",
        )

        assert result["status"] == "error"
        assert result["is_paper"] is False

    def test_live_requires_50_paper_trades(self, db_engine):
        """Live mode with <50 paper trades falls back to paper."""
        Base.metadata.create_all(db_engine)
        with get_session(db_engine) as session:
            settings = TradingSettings()
            settings.mode = "live"
            settings.paper_trade_count = 10  # Under threshold
            session.add(settings)
            session.add(Market(
                market_id="TEST-MKT", title="Test",
                category="General",
                close_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
                status="open",
            ))
            session.commit()

        engine = TradeEngine(db_engine)
        assert engine.can_trade_live() is False

        decision = _make_decision()
        result = engine.execute(
            decision=decision, market_id="TEST-MKT",
            p_model=0.60, implied_prob=0.50, edge=0.10, net_ev=0.05,
            confidence=0.80, reasoning="test fallback",
        )

        # Should fall back to paper
        assert result["is_paper"] is True
        assert result["status"] == "filled"

    def test_live_no_client_raises(self, db_engine):
        """Live mode without client raises RuntimeError."""
        _setup_live_ready(db_engine)

        engine = TradeEngine(db_engine)  # No client
        decision = _make_decision()

        with pytest.raises(RuntimeError, match="KalshiClient"):
            engine.execute(
                decision=decision, market_id="TEST-MKT",
                p_model=0.60, implied_prob=0.50, edge=0.10, net_ev=0.05,
                confidence=0.80, reasoning="test no client",
            )
