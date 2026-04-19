from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel


class KalshiMarket(BaseModel):
    ticker: str
    title: str
    category: str
    sub_title: str = ""
    close_time: datetime
    status: str
    rules_primary: str = ""
    yes_bid: int = 0
    yes_ask: int = 0
    last_price: int = 0
    volume: int = 0
    volume_24h: int = 0


class KalshiMarketsResponse(BaseModel):
    markets: List[KalshiMarket]
    cursor: str = ""


class KalshiOrderbook(BaseModel):
    yes: List[List[int]]
    no: List[List[int]]
