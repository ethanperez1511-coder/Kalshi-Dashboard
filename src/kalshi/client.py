from __future__ import annotations

import asyncio
import logging
from typing import List

import httpx

from src.kalshi.schemas import KalshiMarket, KalshiMarketsResponse, KalshiOrderbook

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self._http = http_client
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(self._max_retries):
            response = await self._http.request(method, path, **kwargs)
            if response.status_code == 429:
                delay = self._retry_delay * (2 ** attempt)
                logger.warning(
                    f"Rate limited, retrying in {delay}s (attempt {attempt + 1})"
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def get_markets(self, cursor: str = "", limit: int = 100) -> List[KalshiMarket]:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._request("GET", "/markets", params=params)
        data = KalshiMarketsResponse(**response.json())
        return data.markets

    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        response = await self._request("GET", f"/orderbook/{ticker}")
        data = response.json()
        return KalshiOrderbook(**data.get("orderbook", data))
