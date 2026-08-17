from __future__ import annotations

from collections import Counter
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.ev.fills import fill_prices
from src.execution.allowlist import resolve_order_type
from src.models.trade import Trade
from src.models.position import Position
from src.models.settings import TradingSettings
from src.risk.manager import TradeDecision
from src.execution.preflight import assert_maker_allowed
from src.legacy_cutoff import current_deploy_sha
from src.trading.fees import kalshi_fee
from src.trading_config import (
    ORDER_TYPE,
    REQUOTE_SECONDS,
    PAPER_CONSERVATIVE_FILLS,
    SKIP_HELD_MARKETS,
)

logger = logging.getLogger(__name__)

# How long to poll for order fill before cancelling (seconds)
DEFAULT_FILL_TIMEOUT = 300
POLL_INTERVAL = 5


def _run_async(coro):
    """Run a coroutine to completion from sync or async calling contexts.

    Inside a running event loop (e.g. a FastAPI handler), asyncio.run() and
    loop.run_until_complete() both raise — run it on a worker thread instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def sync_live_bankroll(engine: Engine, kalshi_client) -> Optional[float]:
    """Reconcile the DB bankroll against the real Kalshi account.

    Kalshi reports *cash*, which excludes open positions. The bankroll field is
    an equity-at-cost ledger on both paths, so the cost basis of open positions
    is added back before storing. Writing the raw cash figure is what used to
    make live's bankroll mean something different from paper's, and every risk
    limit divides by it.

    Live mode only — the paper bankroll is virtual and must never be
    overwritten by this. Returns the synced dollar amount, or None if no
    client was provided.
    """
    if kalshi_client is None:
        return None
    balance = _run_async(kalshi_client.get_balance())
    cash = balance.balance / 100.0
    with get_session(engine) as session:
        open_cost = sum(
            p.cost_basis
            for p in session.query(Position).filter_by(status="open").all()
        )
        dollars = round(cash + open_cost, 2)
        s = session.query(TradingSettings).first()
        if s:
            s.bankroll = dollars
            if dollars > s.peak_bankroll:
                s.peak_bankroll = dollars
        session.commit()
    logger.info(
        f"Live bankroll synced from Kalshi: ${dollars:.2f} "
        f"(cash ${cash:.2f} + open cost basis ${open_cost:.2f})"
    )
    return dollars


class TradeEngine:
    def __init__(self, engine: Engine, kalshi_client=None):
        self._engine = engine
        self._client = kalshi_client
        self._fill_timeout = DEFAULT_FILL_TIMEOUT
        # Why execute() last returned None, and the running tally. Three paths
        # return None and they mean entirely different things: a routine
        # already-held market and a fill price that no longer matches the one
        # the edge was computed at are not the same event, and reporting them
        # as one number made the second invisible.
        self.refusals: Counter = Counter()
        self.last_refusal: Optional[str] = None

    def _refuse(self, reason: str) -> None:
        self.last_refusal = reason
        self.refusals[reason] += 1

    def _has_open_position(self, market_id: str) -> bool:
        with get_session(self._engine) as session:
            return (
                session.query(Position)
                .filter_by(market_id=market_id, status="open")
                .first()
                is not None
            )

    def _get_mode(self) -> dict:
        with get_session(self._engine) as session:
            s = session.query(TradingSettings).first()
            if not s:
                return {"mode": "paper", "paper_trade_count": 0, "paper_trades_before_live": 50}
            return {
                "mode": s.mode,
                "paper_trade_count": s.paper_trade_count,
                "paper_trades_before_live": s.paper_trades_before_live,
            }

    def can_trade_live(self) -> bool:
        info = self._get_mode()
        if info["mode"] != "live":
            return False
        if info["paper_trade_count"] < info["paper_trades_before_live"]:
            return False
        return True

    def _compute_fill_price(
        self, decision: TradeDecision, yes_bid: int = 0, yes_ask: int = 0,
        is_paper: bool = True, order_type: str = "",
    ) -> int:
        """What this trade costs, from the same function that priced its EV.

        These were two separate implementations and they disagreed by a cent —
        the EV was computed at 100 - last_price and the fill taken at
        100 - yes_bid. See src/ev/fills.py.
        """
        # `order_type` is RESOLVED BY THE CALLER and threaded in. Pricing must
        # not resolve it itself: a second independent read of the order type
        # for one market is the trade 1/50 divergence rebuilt, and it would
        # also give this pure pricing helper a database dependency.
        yes_fill, no_fill = fill_prices(
            decision.price_cents if decision.side == "yes" else 100 - decision.price_cents,
            yes_bid, yes_ask, order_type, is_paper=is_paper,
        )
        return yes_fill if decision.side == "yes" else no_fill

    def execute(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
        yes_bid: int = 0,
        yes_ask: int = 0,
        model_name: str = "",
        traded_edge: Optional[float] = None,
        evaluated_price: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        # A stale reason carried into the next opportunity is worse than none.
        self.last_refusal = None

        if not decision.approved:
            logger.info(f"Trade rejected for {market_id}: {decision.rejection_reasons}")
            self._refuse("risk_rejected")
            return None

        # Skip markets we already hold: re-entering every cycle concentrates risk
        # and burns the paper-trade evaluation on a handful of markets.
        if SKIP_HELD_MARKETS and self._has_open_position(market_id):
            logger.info(f"Skipping {market_id}: position already open")
            self._refuse("position_already_open")
            return None

        # A config flag is not a decision. If maker execution is switched on
        # while the legacy integer money path is live, fractional fills would be
        # truncated rather than rejected — so refuse here, loudly, at the moment
        # it would have traded.
        assert_maker_allowed(self._engine)

        mode_info = self._get_mode()
        is_paper = mode_info["mode"] == "paper" or not self.can_trade_live()

        # Compute realistic fill price (paper uses conservative taker pricing)
        # One resolution for this market, used for the fill price here and
        # matched against the price the scorer evaluated at.
        order_type = resolve_order_type(self._engine, market_id)
        fill_price = self._compute_fill_price(
            decision, yes_bid, yes_ask, is_paper=is_paper, order_type=order_type,
        )

        # A trade that costs a different price than the one that justified it
        # was justified by a different trade. One cent either side of the edge
        # threshold is the whole margin at the size of edge this system trades,
        # so this refuses rather than reconciles.
        if evaluated_price is not None and fill_price != evaluated_price:
            logger.error(
                "Refusing %s: evaluated at %dc but fills at %dc — the edge that "
                "passed the gate is not the edge available",
                market_id, evaluated_price, fill_price,
            )
            self._refuse("fill_price_diverged")
            return None

        if is_paper:
            return self._execute_paper(
                decision, market_id, p_model, implied_prob,
                edge, net_ev, confidence, reasoning, fill_price, model_name,
                traded_edge,
            )
        else:
            if self._client is None:
                raise RuntimeError(
                    "Live trading requires a KalshiClient. "
                    "Pass kalshi_client to TradeEngine."
                )
            return _run_async(
                self._execute_live(
                    decision, market_id, p_model, implied_prob,
                    edge, net_ev, confidence, reasoning, fill_price, model_name,
                    traded_edge,
                )
            )

    def _execute_paper(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
        fill_price: int = 0,
        model_name: str = "",
        traded_edge: Optional[float] = None,
    ) -> Dict[str, Any]:
        actual_price = fill_price if fill_price > 0 else decision.price_cents
        with get_session(self._engine) as session:
            trade = Trade(
                market_id=market_id,
                side=decision.side,
                action="buy",
                price=actual_price,
                quantity=decision.quantity,
                p_model=p_model,
                implied_prob=implied_prob,
                edge=edge,
                traded_edge=traded_edge,
                evaluated_price=actual_price,
                net_ev=net_ev,
                position_size_dollars=decision.position_size_dollars,
                confidence=confidence,
                reasoning=reasoning,
                is_paper=True,
                status="filled",
                # Paper pays no real fee, so record the simulated Kalshi fee now.
                # Settlement consumes this field for both paths, which is what
                # makes paper and live PnL directly comparable.
                entry_fee=kalshi_fee(decision.quantity, actual_price),
                entry_fee_source="simulated",
                model_name=model_name or None,
                deploy_sha=current_deploy_sha(),
            )
            session.add(trade)

            existing_pos = (
                session.query(Position)
                .filter_by(market_id=market_id, side=decision.side, status="open")
                .first()
            )
            if existing_pos:
                existing_pos.quantity += decision.quantity
            else:
                pos = Position(
                    market_id=market_id,
                    side=decision.side,
                    entry_price=actual_price,
                    quantity=decision.quantity,
                    current_price=actual_price,
                    status="open",
                )
                session.add(pos)

            settings = session.query(TradingSettings).first()
            if settings:
                settings.paper_trade_count += 1

            session.commit()

            logger.info(
                f"Paper trade executed: {market_id} {decision.side} "
                f"x{decision.quantity} @ {actual_price}c "
                f"(${decision.position_size_dollars:.2f})"
            )

            return {
                "market_id": market_id,
                "side": decision.side,
                "price": actual_price,
                "quantity": decision.quantity,
                "dollars": decision.position_size_dollars,
                "is_paper": True,
                "status": "filled",
            }

    async def _execute_live(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
        fill_price: int = 0,
        model_name: str = "",
        traded_edge: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Limit price in the order's own side terms (maker price from
        # _compute_fill_price); fall back to the decision price.
        limit_price = fill_price if fill_price > 0 else decision.price_cents

        # 1. Create pending trade record BEFORE API call
        trade_id = self._create_pending_trade(
            decision, market_id, p_model, implied_prob,
            edge, net_ev, confidence, reasoning, limit_price, model_name,
            traded_edge,
        )

        order_id = None
        try:
            # 2. Place limit order via Kalshi API
            resp = await self._client.place_order(
                ticker=market_id,
                side=decision.side,
                count=decision.quantity,
                price_cents=limit_price,
                action="buy",
            )
            order_id = resp.order_id
            self._update_trade_order_id(trade_id, order_id)
            logger.info(
                f"Live order placed: {market_id} {decision.side} "
                f"x{decision.quantity} @ {limit_price}c — order_id={order_id}"
            )

            # 3. Poll for fill; on timeout count any partial fill
            filled_qty = await self._poll_for_fill(order_id, decision.quantity)

            if filled_qty == decision.quantity:
                fee, fee_source = await self._fetch_entry_fee(
                    order_id, filled_qty, limit_price
                )
                self._mark_trade_filled(
                    trade_id, decision, market_id, limit_price, filled_qty,
                    entry_fee=fee, entry_fee_source=fee_source,
                )
                logger.info(f"Live order FILLED: {order_id}")
                status = "filled"
            elif filled_qty > 0:
                # Partial fill at timeout: keep what filled, cancel the rest.
                await self._cancel_order_safe(order_id)
                fee, fee_source = await self._fetch_entry_fee(
                    order_id, filled_qty, limit_price
                )
                self._mark_trade_filled(
                    trade_id, decision, market_id, limit_price, filled_qty,
                    entry_fee=fee, entry_fee_source=fee_source,
                )
                logger.warning(
                    f"Live order PARTIALLY filled ({filled_qty}/{decision.quantity}), "
                    f"remainder cancelled: {order_id}"
                )
                status = "partial"
            else:
                await self._cancel_order_safe(order_id)
                self._update_trade_status(trade_id, "cancelled")
                logger.warning(f"Live order TIMED OUT and cancelled: {order_id}")
                status = "cancelled"

            return {
                "market_id": market_id,
                "side": decision.side,
                "price": limit_price,
                "quantity": filled_qty if status == "partial" else decision.quantity,
                "dollars": round(limit_price * filled_qty / 100.0, 2),
                "is_paper": False,
                "status": status,
                "order_id": order_id,
            }

        except Exception:
            logger.exception(f"Live trade error for {market_id} (order_id={order_id})")
            if order_id:
                await self._cancel_order_safe(order_id)
            self._update_trade_status(trade_id, "error")
            return {
                "market_id": market_id,
                "side": decision.side,
                "price": limit_price,
                "quantity": decision.quantity,
                "dollars": decision.position_size_dollars,
                "is_paper": False,
                "status": "error",
                "order_id": order_id,
            }

    def _create_pending_trade(
        self, decision, market_id, p_model, implied_prob,
        edge, net_ev, confidence, reasoning, limit_price: int = 0,
        model_name: str = "", traded_edge: Optional[float] = None,
    ) -> int:
        with get_session(self._engine) as session:
            trade = Trade(
                market_id=market_id,
                side=decision.side,
                action="buy",
                price=limit_price if limit_price > 0 else decision.price_cents,
                quantity=decision.quantity,
                p_model=p_model,
                implied_prob=implied_prob,
                edge=edge,
                traded_edge=traded_edge,
                evaluated_price=limit_price if limit_price > 0 else decision.price_cents,
                net_ev=net_ev,
                position_size_dollars=decision.position_size_dollars,
                confidence=confidence,
                reasoning=reasoning,
                is_paper=False,
                status="pending",
                model_name=model_name or None,
                deploy_sha=current_deploy_sha(),
            )
            session.add(trade)
            session.commit()
            return trade.id

    def _update_trade_order_id(self, trade_id: int, order_id: str):
        with get_session(self._engine) as session:
            trade = session.get(Trade, trade_id)
            if trade:
                trade.order_id = order_id
            session.commit()

    def _update_trade_status(self, trade_id: int, status: str):
        with get_session(self._engine) as session:
            trade = session.get(Trade, trade_id)
            if trade:
                trade.status = status
            session.commit()

    async def _fetch_entry_fee(
        self, order_id: Optional[str], quantity: int, price_cents: int,
    ) -> tuple:
        """Real Kalshi entry fee in dollars for a live fill, plus its provenance.

        Kalshi charges the fee at entry, so it never appears in settlement data —
        the fills endpoint is the only place the true number lives. If that
        lookup fails we fall back to the published formula and label the result
        an estimate. Never returns 0.0 on failure: a silent zero is exactly the
        bug this change exists to kill.
        """
        estimate = kalshi_fee(quantity, price_cents)
        if self._client is None or not order_id:
            return estimate, "estimated"
        try:
            fills = await self._client.get_fills(order_id=order_id)
            if not fills:
                raise ValueError(f"no fills returned for order {order_id}")
            cents = sum(int(f.fee) for f in fills)
            return round(cents / 100.0, 4), "kalshi_fills"
        except Exception:
            logger.warning(
                f"Could not read real fee for order {order_id}; "
                f"using estimate ${estimate:.2f}", exc_info=True,
            )
            return estimate, "estimated"

    def _mark_trade_filled(
        self, trade_id: int, decision, market_id: str,
        fill_price: int, filled_qty: int,
        entry_fee: float = 0.0, entry_fee_source: str = "estimated",
    ):
        """Record a (possibly partial) live fill.

        fill_price is in the order's own side terms — positions store
        side-cost everywhere. Bankroll is debited the actual fill cost plus the
        entry fee, matching the cash Kalshi actually takes.
        """
        with get_session(self._engine) as session:
            trade = session.get(Trade, trade_id)
            if trade:
                trade.status = "filled"
                trade.quantity = filled_qty
                trade.entry_fee = entry_fee
                trade.entry_fee_source = entry_fee_source

            # Create or update position
            existing_pos = (
                session.query(Position)
                .filter_by(market_id=market_id, side=decision.side, status="open")
                .first()
            )
            if existing_pos:
                existing_pos.quantity += filled_qty
            else:
                pos = Position(
                    market_id=market_id,
                    side=decision.side,
                    entry_price=fill_price,
                    quantity=filled_qty,
                    current_price=fill_price,
                    status="open",
                )
                session.add(pos)

            # The bankroll is deliberately NOT moved here. It is an
            # equity-at-cost ledger on both paths — buying swaps cash for a
            # position of equal cost, so equity is unchanged — and it moves only
            # at settlement, by realized PnL (which is already net of this fee).
            # Debiting here is what made live's ledger incomparable to paper's.
            session.commit()

    async def _poll_for_fill(self, order_id: str, quantity: int) -> int:
        """Poll until the order fills, is cancelled, or times out.

        Returns the number of contracts filled (0..quantity) — a timeout
        with a partial fill reports the filled portion, not zero.
        """
        def _filled_from(order: dict) -> int:
            remaining = order.get("remaining_count", quantity)
            return max(0, quantity - remaining)

        order = {}
        elapsed = 0
        while elapsed < self._fill_timeout:
            order_data = await self._client.get_order(order_id)
            order = order_data.get("order", order_data)
            status = order.get("status", "")
            remaining = order.get("remaining_count", 1)

            if status == "executed" or remaining == 0:
                return quantity
            if status in ("canceled", "cancelled"):
                return _filled_from(order)

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        # Timed out — report whatever has filled so far.
        try:
            order_data = await self._client.get_order(order_id)
            order = order_data.get("order", order_data)
        except Exception:
            logger.exception(f"Final order state fetch failed for {order_id}")
        return _filled_from(order)

    async def _cancel_order_safe(self, order_id: str):
        try:
            await self._client.cancel_order(order_id)
        except Exception:
            logger.exception(f"Failed to cancel order {order_id}")
