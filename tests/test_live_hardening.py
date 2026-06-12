"""Tests for live-path hardening (2026-06-11 session 2).

Bugs being locked down:
- place_order always sent yes_price, so a live NO order "at 85c" would land
  as yes_price=85 (NO cost 15c) — the wrong side of the book.
- _execute_live placed orders at decision.price_cents (scorer's last_price)
  instead of the computed maker fill price, and stored YES-scale entry on
  NO positions.
- execute() used asyncio.get_event_loop().run_until_complete, which raises
  inside any running event loop (e.g. a FastAPI handler).
- Partial fills at timeout were recorded as fully cancelled.
- Live bankroll was never synced from the real Kalshi balance.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.database import get_session, Base
from src.kalshi.client import KalshiClient
from src.kalshi.schemas import CreateOrderResponse, KalshiBalance
from src.models.market import Market
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.risk.manager import TradeDecision
from src.trading.engine import TradeEngine


def _setup_live_ready(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.mode = "live"
        settings.paper_trade_count = 55
        settings.paper_trades_before_live = 50
        settings.bankroll = 100.0
        session.add(settings)
        session.add(Market(
            market_id="TEST-MKT", title="Test", category="General",
            close_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
            status="open",
        ))
        session.commit()


def _make_decision(side="yes", price=50, qty=2, dollars=1.0):
    return TradeDecision(
        approved=True, side=side, position_size_dollars=dollars,
        quantity=qty, price_cents=price, rejection_reasons=[],
    )


def _filled_client(order_id="ord-1"):
    client = AsyncMock()
    client.place_order.return_value = CreateOrderResponse(
        order_id=order_id, ticker="TEST-MKT", status="resting",
    )
    client.get_order.return_value = {
        "order": {"order_id": order_id, "status": "executed", "remaining_count": 0}
    }
    return client


# --- B1: place_order sends the correct price field per side ---

class TestPlaceOrderSides:
    @pytest.fixture
    def mock_client(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        http.base_url = httpx.URL("https://api.kalshi.com/trade-api/v2")
        return KalshiClient(http_client=http)

    def _capture_payload(self, mock_client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "order": {"order_id": "o1", "ticker": "T", "status": "resting"}
        }
        mock_client._request = AsyncMock(return_value=resp)
        return mock_client._request

    def test_no_order_sends_no_price(self, mock_client):
        req = self._capture_payload(mock_client)
        asyncio.run(mock_client.place_order("T", "no", 3, 85))
        payload = req.call_args.kwargs["json"]
        assert payload["no_price"] == 85
        assert "yes_price" not in payload

    def test_yes_order_sends_yes_price(self, mock_client):
        req = self._capture_payload(mock_client)
        asyncio.run(mock_client.place_order("T", "yes", 3, 40))
        payload = req.call_args.kwargs["json"]
        assert payload["yes_price"] == 40
        assert "no_price" not in payload


# --- B2: execute() works from inside a running event loop ---

class TestAsyncBridge:
    def test_execute_live_from_async_context(self, db_engine):
        _setup_live_ready(db_engine)
        client = _filled_client()
        engine = TradeEngine(db_engine, kalshi_client=client)
        engine._fill_timeout = 10

        async def call_from_loop():
            # Synchronous call made while a loop is running — must not raise.
            return engine.execute(
                decision=_make_decision(), market_id="TEST-MKT",
                p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
                confidence=0.8, reasoning="async ctx",
            )

        result = asyncio.run(call_from_loop())
        assert result is not None
        assert result["status"] == "filled"


# --- B3: live orders priced at the maker fill price, side-cost stored ---

class TestLiveOrderPricing:
    def test_no_order_placed_at_maker_side_cost(self, db_engine):
        _setup_live_ready(db_engine)
        client = _filled_client()
        engine = TradeEngine(db_engine, kalshi_client=client)
        engine._fill_timeout = 10

        # NO trade; book yes_bid=48 / yes_ask=52. Maker NO price = 100-52+1 = 49.
        result = engine.execute(
            decision=_make_decision(side="no", price=51, qty=3),
            market_id="TEST-MKT",
            p_model=0.4, implied_prob=0.51, edge=0.1, net_ev=0.05,
            confidence=0.8, reasoning="live no",
            yes_bid=48, yes_ask=52,
        )
        assert result["status"] == "filled"
        assert client.place_order.call_args.kwargs["price_cents"] == 49
        assert client.place_order.call_args.kwargs["side"] == "no"

        with get_session(db_engine) as session:
            pos = session.query(Position).filter_by(market_id="TEST-MKT").one()
            assert pos.side == "no"
            assert pos.entry_price == 49  # side-cost terms
            trade = session.query(Trade).filter_by(market_id="TEST-MKT").one()
            assert trade.price == 49
            settings = session.query(TradingSettings).first()
            # actual cost deducted: 49c x 3 = $1.47
            assert settings.bankroll == pytest.approx(100.0 - 1.47)


# --- B4: partial fill at timeout records the filled portion ---

class TestPartialFill:
    def test_partial_fill_recorded(self, db_engine):
        _setup_live_ready(db_engine)
        client = AsyncMock()
        client.place_order.return_value = CreateOrderResponse(
            order_id="ord-p", ticker="TEST-MKT", status="resting",
        )
        # 1 of 3 contracts filled; order stays resting until timeout.
        client.get_order.return_value = {
            "order": {"order_id": "ord-p", "status": "resting", "remaining_count": 2}
        }
        engine = TradeEngine(db_engine, kalshi_client=client)
        engine._fill_timeout = 0  # immediate timeout

        result = engine.execute(
            decision=_make_decision(side="yes", price=50, qty=3),
            market_id="TEST-MKT",
            p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
            confidence=0.8, reasoning="partial",
            yes_bid=48, yes_ask=52,
        )
        assert result["status"] == "partial"
        assert result["quantity"] == 1
        client.cancel_order.assert_awaited()  # remainder cancelled

        with get_session(db_engine) as session:
            pos = session.query(Position).filter_by(market_id="TEST-MKT").one()
            assert pos.quantity == 1
            trade = session.query(Trade).filter_by(order_id="ord-p").one()
            assert trade.status == "filled"
            assert trade.quantity == 1

    def test_zero_fill_timeout_cancelled(self, db_engine):
        _setup_live_ready(db_engine)
        client = AsyncMock()
        client.place_order.return_value = CreateOrderResponse(
            order_id="ord-z", ticker="TEST-MKT", status="resting",
        )
        client.get_order.return_value = {
            "order": {"order_id": "ord-z", "status": "resting", "remaining_count": 3}
        }
        engine = TradeEngine(db_engine, kalshi_client=client)
        engine._fill_timeout = 0

        result = engine.execute(
            decision=_make_decision(side="yes", price=50, qty=3),
            market_id="TEST-MKT",
            p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
            confidence=0.8, reasoning="no fill",
            yes_bid=48, yes_ask=52,
        )
        assert result["status"] == "cancelled"
        with get_session(db_engine) as session:
            assert session.query(Position).count() == 0
            trade = session.query(Trade).filter_by(order_id="ord-z").one()
            assert trade.status == "cancelled"


# --- B5: live bankroll syncs from the real Kalshi balance ---

class TestBankrollSync:
    def test_sync_updates_bankroll_from_balance(self, db_engine):
        _setup_live_ready(db_engine)
        client = AsyncMock()
        client.get_balance.return_value = KalshiBalance(balance=25050, portfolio_value=30000)

        from src.trading.engine import sync_live_bankroll
        synced = sync_live_bankroll(db_engine, client)
        assert synced == pytest.approx(250.50)
        with get_session(db_engine) as session:
            s = session.query(TradingSettings).first()
            assert s.bankroll == pytest.approx(250.50)

    def test_sync_noop_without_client(self, db_engine):
        _setup_live_ready(db_engine)
        from src.trading.engine import sync_live_bankroll
        assert sync_live_bankroll(db_engine, None) is None
        with get_session(db_engine) as session:
            s = session.query(TradingSettings).first()
            assert s.bankroll == pytest.approx(100.0)  # unchanged
