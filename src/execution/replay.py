"""Rebuild the order book from the recorded tape, or refuse to.

The fill simulator needs to know what the book looked like at a moment: whether
our order was resting, what the touch was, how deep the level was. That state is
not recorded — only a starting snapshot and a stream of deltas are. Replaying
them is the only way to get it back, and replaying them wrong is worse than not
having them, because a book reconstructed from an incomplete stream looks
exactly like a book reconstructed from a complete one.

So this module is mostly refusals. Four things make a reconstruction
unprovable, and each one stops the replay rather than degrading it:

  gap          a sequence number skipped. Every delta after it is applied to a
               base we no longer know. `seq` is per-subscription, so a
               reconnect legitimately restarts the numbering — that is a new
               sid, not a gap.
  no base      a delta before any snapshot on this sid. There is nothing to
               apply it to.
  negative     a delta driving a level below zero. Quantities cannot be
               negative, so this is proof a message was missed even when the
               sequence numbers look continuous.
  unreadable   a payload whose shape we cannot parse.

The alternative — carrying on and hoping — produces a book that is quietly
wrong, and the simulator downstream cannot tell. A refusal costs a window of
data. A silent error costs the meaning of every number computed from it.

Nothing here reads the day-7 measurement window or produces a capture estimate.
It is the substrate those measurements will run on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Which side of the book a level belongs to. Kalshi quotes both YES and NO.
SIDES = ("yes", "no")


class ReplayRefused(Exception):
    """The book cannot be reconstructed across this point.

    Carries the reason so a caller reporting coverage can say which windows
    were dropped and why, rather than reporting a smaller number with no
    explanation.
    """

    def __init__(self, reason: str, market_ticker: str = "", seq: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.market_ticker = market_ticker
        self.seq = seq


@dataclass(frozen=True)
class BookState:
    """One side-indexed view of resting size, in contracts, by price in cents."""

    ts_ms: Optional[int]
    seq: Optional[int]
    sid: Optional[int]
    levels: Dict[str, Dict[int, int]]

    def best(self, side: str) -> Optional[Tuple[int, int]]:
        """(price_cents, quantity) at the top of `side`, or None if empty.

        Best for both sides is the HIGHEST price with resting size: these are
        bids in their own side's terms — a resting YES bid at 40 and a resting
        NO bid at 60 are the two halves of the same 40/40 market.
        """
        live = {p: q for p, q in self.levels.get(side, {}).items() if q > 0}
        if not live:
            return None
        price = max(live)
        return price, live[price]

    def depth_at(self, side: str, price_cents: int) -> int:
        return self.levels.get(side, {}).get(price_cents, 0)

    def total(self, side: str) -> int:
        return sum(q for q in self.levels.get(side, {}).values() if q > 0)


def _cents(value) -> Optional[int]:
    """Kalshi publishes prices as dollars; the book is quoted in whole cents."""
    if value is None:
        return None
    cents = round(float(value) * 100)
    if not 0 <= cents <= 100:
        return None
    return int(cents)


def _parse_snapshot(msg: dict) -> Dict[str, Dict[int, int]]:
    """Full book from an orderbook_snapshot message.

    Kalshi sends `{"yes": [[price, qty], ...], "no": [...]}`. Prices arrive as
    dollars in the newer schema and as whole cents in the older one, so both are
    accepted — but only where the result lands in [0, 100]. A price outside that
    range is not a unit ambiguity we can resolve, and guessing which convention
    produced it is how a book silently ends up scaled by 100.
    """
    levels: Dict[str, Dict[int, int]] = {side: {} for side in SIDES}
    for side in SIDES:
        rows = msg.get(side)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ReplayRefused(f"snapshot {side} side is not a list")
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise ReplayRefused(f"unreadable snapshot level on {side}: {row!r}")
            raw_price, raw_qty = row[0], row[1]
            price = _cents(raw_price) if isinstance(raw_price, float) else None
            if price is None:
                try:
                    price = int(raw_price)
                except (TypeError, ValueError):
                    raise ReplayRefused(f"unreadable price on {side}: {raw_price!r}")
            if not 0 <= price <= 100:
                raise ReplayRefused(f"price {price} out of range on {side}")
            try:
                quantity = int(raw_qty)
            except (TypeError, ValueError):
                raise ReplayRefused(f"unreadable quantity on {side}: {raw_qty!r}")
            if quantity < 0:
                raise ReplayRefused(f"negative quantity {quantity} in snapshot on {side}")
            levels[side][price] = quantity
    return levels


class BookReplay:
    """Replay recorded messages for one market into successive book states.

    Stateful and single-market by design. Interleaving markets in one replay
    would mean sharing sequence state between subscriptions that do not share
    a numbering.
    """

    def __init__(self, market_ticker: str = ""):
        self.market_ticker = market_ticker
        self._levels: Dict[str, Dict[int, int]] = {side: {} for side in SIDES}
        self._sid: Optional[int] = None
        self._last_seq: Optional[int] = None
        self._has_base = False
        self.applied = 0
        self.trades_seen = 0

    # -- state -----------------------------------------------------------

    def state(self, ts_ms: Optional[int] = None) -> BookState:
        return BookState(
            ts_ms=ts_ms, seq=self._last_seq, sid=self._sid,
            levels={side: dict(levels) for side, levels in self._levels.items()},
        )

    @property
    def has_base(self) -> bool:
        """Has a snapshot established what the deltas apply to?"""
        return self._has_base

    # -- ingestion -------------------------------------------------------

    def apply(self, row) -> Optional[BookState]:
        """Apply one recorded row. Returns the resulting state, or None for a
        message that carries no book change (a trade print).

        Raises ReplayRefused when the book can no longer be proven.
        """
        msg_type = (getattr(row, "msg_type", "") or "").lower()
        sid = getattr(row, "sid", None)
        seq = getattr(row, "seq", None)
        ts_ms = getattr(row, "ts_ms", None)

        if msg_type == "trade":
            # Prints do not move resting size on their own; the exchange sends
            # the corresponding delta separately. Counting them here keeps the
            # tape and the book from being conflated downstream.
            self.trades_seen += 1
            return None

        self._check_sequence(sid, seq)

        body = self._body(row)
        if msg_type == "snapshot":
            self._levels = _parse_snapshot(body)
            self._has_base = True
        elif msg_type == "delta":
            self._apply_delta(body, seq)
        else:
            raise ReplayRefused(
                f"unknown message type {msg_type!r}", self.market_ticker, seq,
            )

        self._sid, self._last_seq = sid, seq
        self.applied += 1
        return self.state(ts_ms)

    def _body(self, row) -> dict:
        raw = getattr(row, "payload", "") or ""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            raise ReplayRefused("payload is not JSON", self.market_ticker,
                                getattr(row, "seq", None))
        body = message.get("msg", message)
        if not isinstance(body, dict):
            raise ReplayRefused("payload has no message body", self.market_ticker,
                                getattr(row, "seq", None))
        return body

    def _check_sequence(self, sid, seq) -> None:
        if sid != self._sid:
            # A new subscription restarts numbering from scratch. That is a
            # reconnect, not a gap — but it also invalidates the book, because
            # nothing says the two subscriptions saw the same state.
            self._sid = sid
            self._last_seq = None
            self._has_base = False
            self._levels = {side: {} for side in SIDES}
            return
        if seq is None or self._last_seq is None:
            return
        if seq != self._last_seq + 1:
            raise ReplayRefused(
                f"sequence gap: expected {self._last_seq + 1}, got {seq}",
                self.market_ticker, seq,
            )

    def _apply_delta(self, body: dict, seq) -> None:
        if not self._has_base:
            raise ReplayRefused(
                "delta before any snapshot — nothing to apply it to",
                self.market_ticker, seq,
            )
        side = (body.get("side") or "").lower()
        if side not in SIDES:
            raise ReplayRefused(f"delta has no readable side: {side!r}",
                                self.market_ticker, seq)

        price = body.get("price")
        price_cents = int(price) if price is not None else _cents(body.get("price_dollars"))
        if price_cents is None or not 0 <= price_cents <= 100:
            raise ReplayRefused("delta has no readable price",
                                self.market_ticker, seq)

        change = body.get("delta")
        if change is None:
            change = body.get("delta_fp")
        if change is None:
            raise ReplayRefused("delta has no size change", self.market_ticker, seq)

        current = self._levels[side].get(price_cents, 0)
        updated = current + int(change)
        if updated < 0:
            # Resting size cannot go negative. Continuous sequence numbers say
            # we missed nothing; arithmetic says we did. Trust the arithmetic —
            # clamping to zero here would hide the only evidence available that
            # the recorded stream is incomplete.
            raise ReplayRefused(
                f"delta drives {side} {price_cents}c to {updated}: a message was "
                "missed despite unbroken sequence numbers",
                self.market_ticker, seq,
            )
        self._levels[side][price_cents] = updated


@dataclass
class ReplayCoverage:
    """What was reconstructable, and what was not, and why.

    Reported alongside any number derived from a replay. A capture estimate
    computed over 40% of a window is a different claim from one computed over
    all of it, and pooling them into a single figure is the mistake this whole
    subsystem exists to avoid.
    """

    market_ticker: str = ""
    states: int = 0
    messages_applied: int = 0
    trades_seen: int = 0
    refusals: List[str] = field(default_factory=list)

    @property
    def refused(self) -> int:
        return len(self.refusals)

    def summary(self) -> str:
        line = (
            f"{self.market_ticker or 'replay'}: {self.states} book states from "
            f"{self.messages_applied} messages, {self.trades_seen} prints"
        )
        if self.refusals:
            line += f", {self.refused} REFUSED ({'; '.join(self.refusals[:3])}"
            line += ", ..." if self.refused > 3 else ""
            line += ")"
        return line


def replay(rows: Iterable, market_ticker: str = "",
           stop_on_refusal: bool = False) -> Tuple[List[BookState], ReplayCoverage]:
    """Replay rows into book states, recording every refusal.

    By default a refusal ends the current reconstructable run and the replay
    resumes from the next snapshot — a gap costs the window it spans, not the
    whole recording. `stop_on_refusal=True` stops dead, for callers that need a
    single provably continuous run.
    """
    engine = BookReplay(market_ticker)
    coverage = ReplayCoverage(market_ticker=market_ticker)
    states: List[BookState] = []

    for row in rows:
        try:
            state = engine.apply(row)
        except ReplayRefused as refusal:
            coverage.refusals.append(refusal.reason)
            logger.warning("Replay refused for %s: %s", market_ticker, refusal.reason)
            if stop_on_refusal:
                break
            # Invalidate the base: everything after a refusal is unproven until
            # a fresh snapshot re-establishes it. Counts carry across the reset —
            # they describe the recording, not the engine instance, and resetting
            # them would under-report coverage by exactly the work already done.
            coverage.messages_applied += engine.applied
            coverage.trades_seen += engine.trades_seen
            engine = BookReplay(market_ticker)
            continue
        if state is not None:
            states.append(state)

    coverage.states = len(states)
    coverage.messages_applied += engine.applied
    coverage.trades_seen += engine.trades_seen
    return states, coverage


def load_rows(engine, market_ticker: str, start_ms: Optional[int] = None,
              end_ms: Optional[int] = None) -> List:
    """Recorded rows for one market in sequence order.

    Ordered by `id` — insertion order — rather than by `seq`, deliberately.
    Ordering by seq would sort a gap out of existence by renumbering around it,
    and the gap is the thing we most need to see.
    """
    from sqlalchemy import select

    from src.database import get_session
    from src.models.orderbook_raw import OrderbookDeltaRaw

    query = select(OrderbookDeltaRaw).where(
        OrderbookDeltaRaw.market_ticker == market_ticker
    )
    if start_ms is not None:
        query = query.where(OrderbookDeltaRaw.ts_ms >= start_ms)
    if end_ms is not None:
        query = query.where(OrderbookDeltaRaw.ts_ms <= end_ms)

    with get_session(engine) as session:
        rows = session.execute(query.order_by(OrderbookDeltaRaw.id)).scalars().all()
        # Detach into plain records: the caller replays outside the session.
        return [
            _Row(
                msg_type=r.msg_type, sid=r.sid, seq=r.seq, ts_ms=r.ts_ms,
                payload=r.payload,
            )
            for r in rows
        ]


@dataclass(frozen=True)
class _Row:
    msg_type: str
    sid: Optional[int]
    seq: Optional[int]
    ts_ms: Optional[int]
    payload: str
