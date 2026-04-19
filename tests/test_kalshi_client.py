import httpx
import pytest
from src.kalshi.client import KalshiClient


@pytest.fixture
def mock_markets_response():
    return {
        "markets": [
            {
                "ticker": "FED-RATE-JUL",
                "title": "Will Fed raise rates in July?",
                "category": "Economics",
                "sub_title": "",
                "close_time": "2026-07-15T20:00:00Z",
                "status": "open",
                "rules_primary": "Resolves Yes if...",
                "yes_bid": 65,
                "yes_ask": 67,
                "last_price": 66,
                "volume": 50000,
                "volume_24h": 1500,
            }
        ],
        "cursor": "",
    }


@pytest.fixture
def mock_orderbook_response():
    return {
        "orderbook": {
            "yes": [[65, 100], [64, 200]],
            "no": [[35, 150], [36, 300]],
        }
    }


@pytest.mark.asyncio
async def test_get_markets(mock_markets_response):
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_markets_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        markets = await client.get_markets()
        assert len(markets) == 1
        assert markets[0].ticker == "FED-RATE-JUL"
        assert markets[0].yes_bid == 65


@pytest.mark.asyncio
async def test_get_orderbook(mock_orderbook_response):
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_orderbook_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http)
        orderbook = await client.get_orderbook("FED-RATE-JUL")
        assert len(orderbook.yes) == 2
        assert orderbook.yes[0] == [65, 100]


@pytest.mark.asyncio
async def test_get_markets_handles_rate_limit():
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://fake.api") as http:
        client = KalshiClient(http_client=http, max_retries=2, retry_delay=0.01)
        markets = await client.get_markets()
        assert markets == []
        assert call_count == 2
