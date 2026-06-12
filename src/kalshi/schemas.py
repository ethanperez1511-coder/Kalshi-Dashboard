from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator, model_validator


class KalshiMarket(BaseModel):
    ticker: str
    title: str
    category: str = ""
    sub_title: str = ""
    close_time: datetime
    status: str
    rules_primary: str = ""
    event_ticker: str = ""
    # API returns dollars as strings; we convert to integer cents internally.
    yes_bid: int = 0
    yes_ask: int = 0
    last_price: int = 0
    volume: int = 0
    volume_24h: int = 0
    # Raw dollar fields from API (used for conversion only)
    yes_bid_dollars: str = "0"
    yes_ask_dollars: str = "0"
    last_price_dollars: str = "0"
    volume_fp: str = "0"
    volume_24h_fp: str = "0"

    @model_validator(mode="after")
    def _convert_dollars_to_cents(self) -> "KalshiMarket":
        """Convert dollar-denominated API fields to integer cents."""
        if self.yes_bid == 0 and float(self.yes_bid_dollars) > 0:
            self.yes_bid = round(float(self.yes_bid_dollars) * 100)
        if self.yes_ask == 0 and float(self.yes_ask_dollars) > 0:
            self.yes_ask = round(float(self.yes_ask_dollars) * 100)
        if self.last_price == 0 and float(self.last_price_dollars) > 0:
            self.last_price = round(float(self.last_price_dollars) * 100)
        if self.volume == 0 and float(self.volume_fp) > 0:
            self.volume = int(float(self.volume_fp))
        if self.volume_24h == 0 and float(self.volume_24h_fp) > 0:
            self.volume_24h = int(float(self.volume_24h_fp))
        # Derive category from event_ticker if not provided
        if not self.category and self.event_ticker:
            self.category = _category_from_event_ticker(self.event_ticker)
        return self


def _category_from_event_ticker(event_ticker: str) -> str:
    """Extract a rough category from Kalshi event ticker prefix."""
    et = event_ticker.upper()
    if "SPORT" in et:
        return "Sports"
    if "FINANCE" in et or "SP500" in et or "NASDAQ" in et:
        return "Finance"
    if "CRYPTO" in et or "BTC" in et or "ETH" in et:
        return "Crypto"
    if "WEATHER" in et or "RAIN" in et or "TEMP" in et:
        return "Weather"
    if "ECON" in et or "GDP" in et or "CPI" in et or "FED" in et:
        return "Economics"
    if "POLITIC" in et or "ELECT" in et:
        return "Politics"
    return "General"


class KalshiMarketsResponse(BaseModel):
    markets: List[KalshiMarket]
    cursor: str = ""


class KalshiOrderbook(BaseModel):
    yes: List[List[int]]
    no: List[List[int]]


class CreateOrderRequest(BaseModel):
    ticker: str
    side: str  # "yes" or "no"
    type: str = "limit"
    action: str = "buy"
    count: int
    # Exactly one of yes_price / no_price is set, matching `side` — the price
    # is always in the order's own side terms.
    yes_price: Optional[int] = None  # cents
    no_price: Optional[int] = None  # cents
    expiration_ts: Optional[int] = None


class CreateOrderResponse(BaseModel):
    order_id: str
    ticker: str
    status: str
    remaining_count: int = 0
    created_time: str = ""
    action: str = ""
    side: str = ""
    type: str = ""
    yes_price: int = 0
    no_price: int = 0


class CancelOrderResponse(BaseModel):
    order_id: str
    reduced_by: int = 0


class KalshiPosition(BaseModel):
    ticker: str
    position: int = 0
    total_traded: int = 0
    realized_pnl: int = 0  # cents


class KalshiBalance(BaseModel):
    balance: int = 0  # cents
    portfolio_value: int = 0  # cents


class KalshiFill(BaseModel):
    fill_id: str = ""
    order_id: str = ""
    ticker: str = ""
    side: str = ""
    count: int = 0
    yes_price: int = 0
    no_price: int = 0
    fee: int = 0  # cents
    created_time: str = ""
