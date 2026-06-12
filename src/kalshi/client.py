from __future__ import annotations

import asyncio
import logging
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from src.kalshi.auth import KalshiAuth
from src.kalshi.schemas import (
    KalshiMarket, KalshiMarketsResponse, KalshiOrderbook,
    CreateOrderRequest, CreateOrderResponse, CancelOrderResponse,
    KalshiPosition, KalshiBalance, KalshiFill,
)

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        auth: Optional[KalshiAuth] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self._http = http_client
        self._auth = auth
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    @classmethod
    def from_settings(cls, settings) -> KalshiClient:
        auth = None
        if not settings.is_offline_mode and (settings.KALSHI_PRIVATE_KEY_PATH or settings.KALSHI_PRIVATE_KEY):
            auth = KalshiAuth(
                api_key=settings.KALSHI_API_KEY,
                private_key_path=settings.KALSHI_PRIVATE_KEY_PATH,
                private_key_pem=settings.KALSHI_PRIVATE_KEY,
            )
        client = httpx.AsyncClient(base_url=settings.KALSHI_BASE_URL, timeout=30.0)
        return cls(http_client=client, auth=auth)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if self._auth is None:
            return {}
        return self._auth.sign_request(method, path)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        # Build the full path for signing (base_url path + request path)
        base_path = urlparse(str(self._http.base_url)).path.rstrip("/")
        sign_path = f"{base_path}{path}"
        headers.update(self._auth_headers(method.upper(), sign_path))

        for attempt in range(self._max_retries):
            response = await self._http.request(method, path, headers=headers, **kwargs)
            if response.status_code == 429:
                delay = self._retry_delay * (2 ** attempt)
                logger.warning(
                    f"Rate limited, retrying in {delay}s (attempt {attempt + 1})"
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        response = await self._http.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    async def get_markets(self, cursor: str = "", limit: int = 100) -> List[KalshiMarket]:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._request("GET", "/markets", params=params)
        data = KalshiMarketsResponse(**response.json())
        return data.markets

    async def get_all_markets(self, limit: int = 100, max_markets: int = 1000) -> List[KalshiMarket]:
        all_markets: List[KalshiMarket] = []
        cursor = ""
        while True:
            params: dict = {"limit": limit, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            response = await self._request("GET", "/markets", params=params)
            body = response.json()
            data = KalshiMarketsResponse(**body)
            all_markets.extend(data.markets)
            cursor = data.cursor
            if not cursor or len(all_markets) >= max_markets:
                break
            logger.info(f"Fetched {len(all_markets)} markets so far...")
        all_markets = all_markets[:max_markets]
        logger.info(f"Fetched {len(all_markets)} total markets (cap={max_markets})")
        return all_markets

    async def get_event_markets(
        self,
        exclude_categories: tuple = ("Sports",),
        limit: int = 200,
        max_markets: int = 2000,
    ) -> List[KalshiMarket]:
        """Fetch open markets via the events feed, keeping the event's category.

        The raw /markets feed is dominated by sports-parlay series, so a capped
        walk never reaches Elections/Politics/Economics markets. The events feed
        carries the true category and can nest markets in one paginated walk.
        """
        markets: List[KalshiMarket] = []
        cursor = ""
        while len(markets) < max_markets:
            params: dict = {
                "limit": limit,
                "status": "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            response = await self._request("GET", "/events", params=params)
            body = response.json()
            for event in body.get("events", []):
                if event.get("category", "") in exclude_categories:
                    continue
                for raw in event.get("markets", []) or []:
                    try:
                        market = KalshiMarket(
                            **{**raw, "category": event.get("category", "")}
                        )
                    except Exception:
                        # Malformed market: skip it — never substitute defaults.
                        logger.warning(
                            f"Skipping malformed nested market "
                            f"{raw.get('ticker', '?')} in {event.get('event_ticker', '?')}"
                        )
                        continue
                    markets.append(market)
                    if len(markets) >= max_markets:
                        break
                if len(markets) >= max_markets:
                    break
            cursor = body.get("cursor", "")
            if not cursor:
                break
        logger.info(f"Fetched {len(markets)} event-category markets (cap={max_markets})")
        return markets

    async def get_event(self, event_ticker: str) -> dict:
        response = await self._request("GET", f"/events/{event_ticker}")
        return response.json()

    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        response = await self._request("GET", f"/orderbook/{ticker}")
        data = response.json()
        return KalshiOrderbook(**data.get("orderbook", data))

    # --- Order / Portfolio Methods ---

    async def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        action: str = "buy",
        expiration_ts: Optional[int] = None,
    ) -> CreateOrderResponse:
        # price_cents is in the order's own side terms; send the matching field.
        req = CreateOrderRequest(
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            yes_price=price_cents if side == "yes" else None,
            no_price=price_cents if side == "no" else None,
            expiration_ts=expiration_ts,
        )
        payload = req.model_dump(exclude_none=True)
        response = await self._request("POST", "/portfolio/orders", json=payload)
        data = response.json()
        return CreateOrderResponse(**data.get("order", data))

    async def cancel_order(self, order_id: str) -> CancelOrderResponse:
        response = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return CancelOrderResponse(**response.json())

    async def get_order(self, order_id: str) -> dict:
        response = await self._request("GET", f"/portfolio/orders/{order_id}")
        return response.json()

    async def get_positions(self) -> List[KalshiPosition]:
        response = await self._request("GET", "/portfolio/positions")
        data = response.json()
        settlements = data.get("market_positions", data.get("positions", []))
        return [KalshiPosition(**p) for p in settlements]

    async def get_balance(self) -> KalshiBalance:
        response = await self._request("GET", "/portfolio/balance")
        return KalshiBalance(**response.json())

    async def get_fills(
        self,
        ticker: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[KalshiFill]:
        params: dict = {}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        response = await self._request("GET", "/portfolio/fills", params=params)
        data = response.json()
        fills = data.get("fills", [])
        return [KalshiFill(**f) for f in fills]
