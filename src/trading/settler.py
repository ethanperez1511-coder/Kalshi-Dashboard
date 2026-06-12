"""Settle open positions by checking if their markets have finalized on Kalshi."""
from __future__ import annotations
import asyncio
import logging
from typing import List, Dict, Any

from sqlalchemy import Engine

from src.database import get_session
from src.models.position import Position
from src.models.market import Market
from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class Settler:
    def __init__(self, engine: Engine, kalshi_client=None):
        self._engine = engine
        self._client = kalshi_client
        self._tracker = PortfolioTracker(engine)

    def settle_all(self) -> List[Dict[str, Any]]:
        """Check all open positions and settle any whose markets have finalized."""
        with get_session(self._engine) as session:
            open_positions = session.query(Position).filter_by(status="open").all()
            market_ids = [p.market_id for p in open_positions]

        if not market_ids:
            return []

        if self._client is None:
            logger.info("No Kalshi client — skipping settlement")
            return []

        # Fetch results from Kalshi API
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = pool.submit(
                    asyncio.run, self._fetch_results(market_ids)
                ).result()
        else:
            results = asyncio.run(self._fetch_results(market_ids))

        settled = []
        for market_id, result in results.items():
            if result not in ("yes", "no"):
                continue

            # Determine exit price: yes=100c if resolved yes, 0c if no
            exit_price = 100 if result == "yes" else 0

            closed = self._tracker.close_position(market_id, exit_price, finalize_market=True)
            if closed:
                settled.append(closed)
                logger.info(
                    f"Settled {market_id}: result={result}, "
                    f"PnL=${closed['realized_pnl']:.2f}"
                )

        return settled

    async def _fetch_results(self, market_ids: List[str]) -> Dict[str, str]:
        """Fetch market result from Kalshi for each market_id."""
        results = {}
        for market_id in market_ids:
            try:
                resp = await self._client._request("GET", f"/markets/{market_id}")
                data = resp.json()
                market = data.get("market", data)
                status = market.get("status", "")
                result = market.get("result", "")
                if status in ("finalized", "settled") and result:
                    results[market_id] = result
            except Exception:
                logger.warning(f"Failed to fetch result for {market_id}", exc_info=True)
        return results
