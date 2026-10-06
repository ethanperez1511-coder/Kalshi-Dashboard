"""Simulate a resting maker order against the recorded tape. Pessimistically.

THE RULE. A resting YES bid at price P fills only when a print satisfies all of:

    taker_outcome_side == "no"          a NO taker consumes resting YES bids
    yes_price          <  P             traded THROUGH, not merely touched
    is_block_trade     == false         block trades match off-book
    our order was resting at that ts    reconstructed from recorded book state
    no sequence gap spans the rest      across a gap, resting is unprovable

No queue assumption is needed anywhere. Kalshi matches on strict price-time
priority and consumes bids best-first, so a taker that reached a price WORSE
than P must have exhausted the entire level at P — including us, wherever we
sat. Queue position is not observable in public data, so any rule requiring it
would be invention.

WHAT THIS UNDERSTATES, and it is a lot. Every partial fill that happens AT our
level without trading through it is discarded. Measured: only 121 of 1,200
taker events (10%) touched two or more price levels. So this recognises roughly
the top decile of taker activity.

The bias is in the COUNT, not the price — the fills it recognises are ones we
would certainly have received. But it over-represents adverse selection by
construction: a trade-through means the market moved decisively against our
resting side, so the fills counted here are disproportionately the ones we
would least like to have had. Both of those distortions point the same way, and
they must be reported separately and never pooled into one PnL number.

The rejected alternative: inferring fills from shrinking book levels. Measured
over 71 seconds, 269 negative deltas of which 250 (92.9%) had no trade at the
same timestamp; cancel volume 25,839 contracts against 297 traded. That would
overstate maker capture by roughly 87x. It is not a fallback.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy import Engine, select

from src.database import get_session
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass(frozen=True)
class TapeTrade:
    """One print, normalised out of the raw feed."""
    ts_ms: int
    yes_price: Decimal          # dollars
    no_price: Decimal
    count: Decimal              # FRACTIONAL — 1541/2299 observed were non-integer
    taker_outcome_side: str
    is_block_trade: bool

    def price_for(self, side: str) -> Decimal:
        return self.yes_price if side == "yes" else self.no_price


@dataclass
class SimulatedFill:
    ts_ms: int
    quantity: Decimal
    price_cents: int


@dataclass
class SimResult:
    filled: Decimal = ZERO
    fills: List[SimulatedFill] = field(default_factory=list)
    unproven: bool = False           # a gap made resting unprovable
    reason: str = ""

    @property
    def any_fill(self) -> bool:
        return self.filled > ZERO


def load_tape(
    engine: Engine, market_id: str, start_ms: int, end_ms: int,
) -> List[TapeTrade]:
    """Recorded prints for one market in a time window."""
    with get_session(engine) as session:
        rows = session.execute(
            select(OrderbookDeltaRaw.payload)
            .where(OrderbookDeltaRaw.market_ticker == market_id)
            .where(OrderbookDeltaRaw.msg_type == "trade")
            .where(OrderbookDeltaRaw.ts_ms >= start_ms)
            .where(OrderbookDeltaRaw.ts_ms <= end_ms)
            .order_by(OrderbookDeltaRaw.ts_ms)
        ).all()

    out: List[TapeTrade] = []
    for (payload,) in rows:
        try:
            body = json.loads(payload).get("msg", {})
            out.append(TapeTrade(
                ts_ms=int(body["ts_ms"]),
                yes_price=Decimal(str(body["yes_price_dollars"])),
                no_price=Decimal(str(body["no_price_dollars"])),
                count=Decimal(str(body["count_fp"])),
                taker_outcome_side=str(body.get("taker_outcome_side", "")),
                is_block_trade=bool(body.get("is_block_trade", False)),
            ))
        except (KeyError, TypeError, ValueError):
            # A message we cannot read is not a fill and is not a zero — skip it
            # and let the coverage numbers show the hole.
            logger.debug("Unparseable tape row for %s", market_id)
    return out


def _dt(ms: int):
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def _first_row_after(engine: Engine, at, before=None):
    with get_session(engine) as session:
        query = (
            select(OrderbookDeltaRaw.received_at)
            .where(OrderbookDeltaRaw.received_at > at)
        )
        if before is not None:
            query = query.where(OrderbookDeltaRaw.received_at < before)
        return session.execute(
            query.order_by(OrderbookDeltaRaw.received_at).limit(1)
        ).scalar()


def has_gap(engine: Engine, market_id: str, start_ms: int, end_ms: int) -> bool:
    """Could the recorded stream have lost a message while our order rested?

    `seq` is per SUBSCRIPTION, and every recorded market shares the same
    subscriptions, so a gap revealed by ANY market's message may have lost a
    print for ours. And a gap is detected at the next message, which can
    arrive after the window ends. So: any gap, on any ticker, detected from
    the window's start until the first row recorded after its end. (Until
    2026-10-06 only gaps tagged with this ticker and inside the window counted.)

    `market_id` is kept for the call signature; the rule no longer narrows on it.
    """
    import datetime as dt

    start, end = _dt(start_ms), _dt(end_ms)
    horizon = _first_row_after(engine, end) or (end + dt.timedelta(minutes=10))
    with get_session(engine) as session:
        return session.execute(
            select(OrderbookGap.id)
            .where(OrderbookGap.detected_at >= start)
            .where(OrderbookGap.detected_at <= horizon)
            .limit(1)
        ).first() is not None


def coverage(engine: Engine, market_id: str, start_ms: int, end_ms: int):
    """Was the recorder demonstrably watching this market for the whole window?

    Returns (covered, reason). Covered means all three:
      * this market was snapshotted before the window opened (it was on the
        subscription when the order began resting)
      * no new snapshot for it inside the window (a reconnect or a new
        recorder segment re-snapshots every market, and leaves a hole)
      * the same connection delivered a row after the window closed (any
        market, before this market's next snapshot), so it did not die midway
    """
    start, end = _dt(start_ms), _dt(end_ms)
    with get_session(engine) as session:
        def snapshot(where, order):
            return session.execute(
                select(OrderbookDeltaRaw.received_at)
                .where(OrderbookDeltaRaw.market_ticker == market_id)
                .where(OrderbookDeltaRaw.msg_type == "snapshot")
                .where(where).order_by(order).limit(1)
            ).scalar()

        anchor = snapshot(OrderbookDeltaRaw.received_at <= start,
                          OrderbookDeltaRaw.received_at.desc())
        if anchor is None:
            return False, "not subscribed before the order began resting"
        inside = snapshot(
            (OrderbookDeltaRaw.received_at > anchor) & (OrderbookDeltaRaw.received_at <= end),
            OrderbookDeltaRaw.received_at,
        )
        if inside is not None:
            return False, "the recorder reconnected or restarted inside the window"
        next_snapshot = snapshot(OrderbookDeltaRaw.received_at > end,
                                 OrderbookDeltaRaw.received_at)

    if _first_row_after(engine, end, before=next_snapshot) is None:
        return False, "no row from the same connection after the window (not yet recorded, or it died)"
    return True, "covered"


def trades_through(trade: TapeTrade, side: str, price_cents: int) -> bool:
    """Does this print consume a resting order at `price_cents` on `side`?"""
    if trade.is_block_trade:
        return False                      # matched off-book, never touched the ladder
    opposite = "no" if side == "yes" else "yes"
    if trade.taker_outcome_side != opposite:
        return False                      # consumed the other side of the book
    limit = Decimal(price_cents) / Decimal(100)
    return trade.price_for(side) < limit  # STRICTLY through, never a touch


def simulate_rest(
    tape: Sequence[TapeTrade],
    side: str,
    price_cents: int,
    quantity: Decimal,
    start_ms: int,
    end_ms: int,
    gap_present: bool = False,
) -> SimResult:
    """Fill an order resting at one price over one interval.

    Fills are capped by the printed size: a 5-contract trade-through cannot
    fill a 10-contract order beyond 5, even though price-time priority
    guarantees our level was reached.
    """
    if gap_present:
        return SimResult(
            unproven=True,
            reason="sequence gap during the resting interval — resting unprovable",
        )

    result = SimResult()
    remaining = quantity
    for trade in tape:
        if remaining <= ZERO:
            break
        if not (start_ms <= trade.ts_ms <= end_ms):
            continue
        if not trades_through(trade, side, price_cents):
            continue
        taken = min(remaining, trade.count)
        remaining -= taken
        result.filled += taken
        result.fills.append(SimulatedFill(trade.ts_ms, taken, price_cents))

    result.reason = (
        f"{result.filled} of {quantity} filled by trade-through"
        if result.any_fill
        else "no qualifying trade-through during the resting interval"
    )
    return result
