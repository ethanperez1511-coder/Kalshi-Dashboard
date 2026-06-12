"""Tests for category-aware event ingest.

The raw /markets feed is dominated by sports-parlay series, so the fetch cap
fills up before any Elections/Politics/Economics market appears. The events
feed carries the true category per event and can nest its markets — that is
the only reliable way to reach non-sports markets.
"""
from __future__ import annotations

import httpx
import pytest

from src.kalshi.client import KalshiClient


def _event(category: str, series: str, n_markets: int = 2) -> dict:
    return {
        "event_ticker": f"{series}-EV",
        "series_ticker": series,
        "category": category,
        "title": f"{series} event",
        "markets": [
            {
                "ticker": f"{series}-M{i}",
                "title": f"{series} market {i}?",
                "close_time": "2026-12-31T20:00:00Z",
                "status": "active",
                "event_ticker": f"{series}-EV",
                "yes_bid_dollars": "0.4000",
                "yes_ask_dollars": "0.4300",
                "last_price_dollars": "0.4100",
                "volume_fp": "1200.00",
            }
            for i in range(n_markets)
        ],
    }


@pytest.fixture
def mock_events_response():
    return {
        "events": [
            _event("Elections", "KXNEWPOPE"),
            _event("Sports", "KXNBAPARLAY"),
            _event("Economics", "KXFED"),
        ],
        "cursor": "",
    }


@pytest.mark.asyncio
async def test_event_markets_skip_sports(mock_events_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("with_nested_markets") == "true"
        return httpx.Response(200, json=mock_events_response)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_event_markets()

    tickers = {m.ticker for m in markets}
    assert "KXNEWPOPE-M0" in tickers
    assert "KXFED-M1" in tickers
    assert not any(t.startswith("KXNBAPARLAY") for t in tickers)


@pytest.mark.asyncio
async def test_event_markets_carry_event_category(mock_events_response):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=mock_events_response)
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_event_markets()

    by_ticker = {m.ticker: m for m in markets}
    assert by_ticker["KXNEWPOPE-M0"].category == "Elections"
    assert by_ticker["KXFED-M0"].category == "Economics"


@pytest.mark.asyncio
async def test_event_markets_convert_dollar_prices(mock_events_response):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=mock_events_response)
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_event_markets()

    m = {m.ticker: m for m in markets}["KXNEWPOPE-M0"]
    assert m.yes_bid == 40
    assert m.yes_ask == 43
    assert m.last_price == 41
    assert m.volume == 1200


@pytest.mark.asyncio
async def test_event_markets_cap_respected():
    big = {
        "events": [_event("Elections", f"KXS{i}", n_markets=1) for i in range(30)],
        "cursor": "",
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=big))
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_event_markets(max_markets=10)
    assert len(markets) == 10


@pytest.mark.asyncio
async def test_event_markets_skip_malformed_market(mock_events_response):
    """A market missing required fields is skipped, never defaulted."""
    mock_events_response["events"][0]["markets"][0].pop("close_time")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=mock_events_response)
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_event_markets()
    tickers = {m.ticker for m in markets}
    assert "KXNEWPOPE-M0" not in tickers
    assert "KXNEWPOPE-M1" in tickers
