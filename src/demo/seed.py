from __future__ import annotations
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.models.market import Market
from src.models.price import PriceSnapshot

logger = logging.getLogger(__name__)

DEMO_MARKETS = [
    {"market_id": "DEMO-FED-RATE-JUL", "title": "Will Fed raise rates in July 2026?", "category": "Economics", "close_date": datetime(2026, 7, 15, tzinfo=timezone.utc), "base_price": 65},
    {"market_id": "DEMO-CPI-UNDER-3", "title": "Will CPI be under 3% in June 2026?", "category": "Economics", "close_date": datetime(2026, 6, 30, tzinfo=timezone.utc), "base_price": 72},
    {"market_id": "DEMO-LAKERS-WIN", "title": "Will Lakers win next game?", "category": "Sports", "close_date": datetime(2026, 4, 25, tzinfo=timezone.utc), "base_price": 45},
    {"market_id": "DEMO-SP500-ABOVE-5200", "title": "Will S&P 500 close above 5200 in April?", "category": "Economics", "close_date": datetime(2026, 4, 30, tzinfo=timezone.utc), "base_price": 55},
    {"market_id": "DEMO-BTC-ABOVE-70K", "title": "Will Bitcoin be above $70k on April 30?", "category": "Crypto", "close_date": datetime(2026, 4, 30, tzinfo=timezone.utc), "base_price": 30},
    {"market_id": "DEMO-RAIN-NYC-FRI", "title": "Will it rain in NYC this Friday?", "category": "Weather", "close_date": datetime(2026, 4, 25, tzinfo=timezone.utc), "base_price": 60},
    {"market_id": "DEMO-GDP-ABOVE-2-5", "title": "Will Q2 GDP growth exceed 2.5%?", "category": "Economics", "close_date": datetime(2026, 7, 31, tzinfo=timezone.utc), "base_price": 40},
]


def seed_demo_data(engine: Engine) -> None:
    now = datetime.now(timezone.utc)
    with get_session(engine) as session:
        for demo in DEMO_MARKETS:
            existing = session.query(Market).filter_by(market_id=demo["market_id"]).first()
            if existing:
                continue
            market = Market(
                market_id=demo["market_id"], title=demo["title"], category=demo["category"],
                close_date=demo["close_date"], status="open",
                rules="Demo market: " + demo["title"],
            )
            session.add(market)
            base = demo["base_price"]
            for i in range(48):
                ts = now - timedelta(minutes=30 * (48 - i))
                drift = random.randint(-3, 3)
                price = max(1, min(99, base + drift))
                spread = random.randint(1, 3)
                snapshot = PriceSnapshot(
                    market_id=demo["market_id"], yes_bid=price, yes_ask=price + spread,
                    last_price=price + random.randint(0, spread),
                    volume=random.randint(100, 5000), timestamp=ts,
                )
                session.add(snapshot)
                base = price
        session.commit()
    logger.info("Demo data seeded")
