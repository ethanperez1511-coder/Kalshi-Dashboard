"""Record the orderbook delta feed. Minimal, robust, append-only.

Deliberately small and deliberately isolated. It subscribes, writes what
arrives, notices when the sequence breaks, and reconnects. It computes nothing,
interprets nothing, and is read by nothing — the simulator consumes this table
later, and keeping the recorder ignorant of the simulator is what stops a
change in fill logic from corrupting the record it is validated against.

Why it exists before the thing that reads it: Kalshi serves no historical book.
The trade tape reaches back ~60 days; the book reaches back zero. Recording
cannot be backfilled, so a day not recorded is a day of validation window that
never exists.

Sequence gaps are recorded, never repaired. A gap means the book cannot be
reconstructed across it, and stitching the numbers back together would hide
precisely the intervals where a simulated fill must not be trusted.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import Engine, and_, select

from src.database import get_session
from src.kalshi.auth import KalshiAuth
from src.maintenance.expire_markets import OPEN_STATUSES
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap
from src.models.position import Position

logger = logging.getLogger(__name__)

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

# Channels worth recording. `trade` is the tape the fill rule keys on; the book
# channel supplies the resting-state evidence that the order was still there.
CHANNELS = ("orderbook_delta", "trade")

# Kalshi documents a per-subscription market cap but publishes no number, so
# this is a self-imposed ceiling: enough for everything we score, small enough
# that hitting an undocumented limit is unlikely.
MAX_MARKETS = 80

RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
FLUSH_EVERY = 200

# A write that fails is retried this many times before the batch is abandoned.
# Bounded on purpose: the alternative to dropping a batch is holding the socket
# hostage to a database that may be down for the rest of the hour.
WRITE_ATTEMPTS = 3
RETRY_SLEEP = 0.5

# Opportunity rows are written every cycle and never invalidated, so without a
# recency bound the subscribe list is every market ever scored. Six hours is
# many cycles of slack while still guaranteeing the list refreshes each run.
RECENT_OPPORTUNITY_HOURS = 6


@dataclass
class RecorderStats:
    messages: int = 0
    written: int = 0
    gaps: int = 0
    reconnects: int = 0
    # A batch the database refused after every retry. Losing a batch is
    # survivable; losing it silently is the bug this counter exists to prevent.
    write_failures: int = 0
    messages_lost: int = 0
    markets: List[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"book recorder: {self.written} messages written across "
            f"{len(self.markets)} markets, {self.gaps} sequence gaps, "
            f"{self.reconnects} reconnects"
        )
        if self.write_failures:
            line += (
                f", {self.write_failures} failed writes losing "
                f"{self.messages_lost} messages"
            )
        return line


def markets_to_record(
    engine: Engine, limit: int = MAX_MARKETS, now: Optional[dt.datetime] = None,
) -> List[str]:
    """Live markets worth recording: anything we hold, plus anything we score.

    Open positions come first and are never dropped by the cap — a market we
    are actually exposed to matters more than one we are merely watching.

    Every candidate must be a market that is still open, joined to its `markets`
    row rather than trusted from the opportunity. Measured 2026-08-16: the
    recorder spent an hour subscribed to KXHIGH*-26AUG13 contracts that had
    settled three days earlier, because Opportunity rows are never invalidated
    and nothing here checked. Those hours are worse than wasted — they inflate
    the day-7 coverage clock with intervals containing no live market.

    A candidate with no `markets` row is dropped rather than recorded. Unknown
    liveness is not a licence: those subscriptions are where the blank-ticker
    rows in the raw table came from.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    live = and_(
        Market.status.in_(OPEN_STATUSES),
        Market.close_date.isnot(None),
        Market.close_date > now,
    )

    with get_session(engine) as session:
        held = [
            row[0] for row in session.execute(
                select(Position.market_id)
                .join(Market, Market.market_id == Position.market_id)
                .where(Position.status == "open", live)
                .distinct()
            ).all()
        ]
        scored = [
            row[0] for row in session.execute(
                select(Opportunity.market_id)
                .join(Market, Market.market_id == Opportunity.market_id)
                .where(
                    Opportunity.status.in_(("qualifying", "watching")),
                    Opportunity.scored_at
                    >= now - dt.timedelta(hours=RECENT_OPPORTUNITY_HOURS),
                    live,
                )
                .order_by(Opportunity.net_ev.desc())
            ).all()
        ]

    ordered: List[str] = []
    for ticker in held + scored:
        if ticker and ticker not in ordered:
            ordered.append(ticker)
    return ordered[:limit]


class BookRecorder:
    def __init__(
        self,
        engine: Engine,
        auth: Optional[KalshiAuth],
        markets: Sequence[str],
        connect=None,
        reconnect_delay: float = RECONNECT_BASE_DELAY,
    ):
        self._engine = engine
        self._auth = auth
        self._markets = list(markets)
        self._connect = connect          # injectable for tests
        self._base_delay = reconnect_delay
        self._last_seq: Dict[int, int] = {}
        self._buffer: List[dict] = []
        self.stats = RecorderStats(markets=list(markets))

    # -- persistence -----------------------------------------------------

    def _attempt_write(self, work, description: str) -> bool:
        """Run one write with bounded retry. True if it became durable.

        Never raises. A database failure must cost at most the batch in hand:
        the production crash was an OperationalError escaping `_flush`, being
        caught by the reconnect handler, which called `_flush` again, which
        raised again — outside the try — and ended a 55-minute recording hour
        that cannot be backfilled.

        The pool is disposed between attempts because the failure mode is a
        connection Neon closed while it sat idle. Retrying with the same dead
        connection just fails identically three times.
        """
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                with get_session(self._engine) as session:
                    work(session)
                return True
            except Exception as exc:
                logger.warning(
                    "%s failed (attempt %d/%d): %s",
                    description, attempt, WRITE_ATTEMPTS, exc,
                )
                try:
                    self._engine.dispose()
                except Exception:  # pragma: no cover - dispose is best effort
                    logger.debug("Engine dispose failed", exc_info=True)
                if attempt < WRITE_ATTEMPTS:
                    time.sleep(RETRY_SLEEP)
        return False

    def _flush(self) -> None:
        if not self._buffer:
            return
        # The buffer is NOT cleared before the insert. It used to be, so a
        # failed write lost the batch even in principle and there was nothing
        # for a retry to retry.
        rows = self._buffer

        def _insert(session):
            session.bulk_insert_mappings(OrderbookDeltaRaw, rows)

        if self._attempt_write(_insert, f"Writing {len(rows)} book messages"):
            self._buffer = []
            self.stats.written += len(rows)
            return

        # Abandon the batch rather than buffer a dead hour into memory — an
        # unbounded buffer is the resource exhaustion that killed the Railway
        # host. The loss is counted and logged at ERROR so it reaches the run
        # summary instead of becoming an unexplained hole in the record.
        self._buffer = []
        self.stats.write_failures += 1
        self.stats.messages_lost += len(rows)
        logger.error(
            "DROPPED %d book messages after %d failed write attempts — this "
            "interval cannot be reconstructed and cannot be backfilled",
            len(rows), WRITE_ATTEMPTS,
        )

    def _record_gap(self, ticker: str, sid: Optional[int], expected: int, got: int):
        def _insert(session):
            session.add(OrderbookGap(
                market_ticker=ticker, sid=sid, expected_seq=expected,
                received_seq=got, missing=max(0, got - expected),
            ))

        if not self._attempt_write(_insert, f"Recording sequence gap on {ticker}"):
            # An unrecorded gap is worse than a recorded one: the replay layer
            # would reconstruct across it believing the sequence was intact.
            logger.error(
                "Could not record the sequence gap on %s (expected %d, got %d) "
                "— the book across this interval must not be trusted",
                ticker, expected, got,
            )
        self.stats.gaps += 1
        logger.warning(
            "Sequence gap on %s (sid=%s): expected %d, got %d — book cannot be "
            "reconstructed across this interval",
            ticker, sid, expected, got,
        )

    # -- message handling ------------------------------------------------

    def handle(self, message: dict) -> None:
        """Buffer one feed message, checking sequence continuity."""
        msg_type = message.get("type", "")
        if msg_type not in ("orderbook_delta", "orderbook_snapshot", "trade"):
            return          # subscribed/ok/error frames are not market data

        body = message.get("msg", {}) or {}
        ticker = body.get("market_ticker") or body.get("ticker") or ""
        sid = message.get("sid")
        seq = message.get("seq")
        self.stats.messages += 1

        if seq is not None and sid is not None:
            previous = self._last_seq.get(sid)
            if previous is not None and seq != previous + 1:
                # A repeat or reordering is still a break in reconstructability.
                self._record_gap(ticker, sid, previous + 1, seq)
            self._last_seq[sid] = seq

        self._buffer.append({
            "market_ticker": ticker,
            "msg_type": "snapshot" if msg_type == "orderbook_snapshot" else (
                "trade" if msg_type == "trade" else "delta"
            ),
            "sid": sid,
            "seq": seq,
            "side": body.get("side"),
            "price_dollars": _as_float(body.get("price_dollars")),
            "delta_fp": _as_float(body.get("delta_fp")),
            "ts_ms": body.get("ts_ms"),
            "payload": json.dumps(message),
            "received_at": dt.datetime.now(dt.timezone.utc),
        })
        if len(self._buffer) >= FLUSH_EVERY:
            self._flush()

    # -- connection ------------------------------------------------------

    def _headers(self) -> dict:
        return self._auth.sign_request("GET", WS_SIGN_PATH) if self._auth else {}

    def _subscriptions(self) -> List[str]:
        return [
            json.dumps({
                "id": index + 1,
                "cmd": "subscribe",
                "params": {"channels": [channel], "market_tickers": self._markets},
            })
            for index, channel in enumerate(CHANNELS)
        ]

    async def run(self, duration_seconds: float) -> RecorderStats:
        """Record until the duration elapses, reconnecting on drop.

        A dropped socket resubscribes from scratch and resets sequence
        tracking: `seq` is per-subscription, so continuing to compare against
        the old numbers would manufacture a gap on every reconnect.
        """
        if not self._markets:
            logger.info("Book recorder: no markets to record")
            return self.stats

        deadline = asyncio.get_event_loop().time() + duration_seconds
        delay = self._base_delay

        while asyncio.get_event_loop().time() < deadline:
            try:
                async with self._open() as socket:
                    for command in self._subscriptions():
                        await socket.send(command)
                    delay = self._base_delay          # a good connection resets backoff

                    while asyncio.get_event_loop().time() < deadline:
                        remaining = deadline - asyncio.get_event_loop().time()
                        raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                        self.handle(json.loads(raw))
            except asyncio.TimeoutError:
                break                                  # duration reached mid-recv
            except Exception as exc:
                self._flush()
                self.stats.reconnects += 1
                self._last_seq.clear()
                logger.warning(
                    "Book recorder disconnected (%s) — reconnecting in %.0fs",
                    exc, delay,
                )
                await asyncio.sleep(min(delay, max(0.0, deadline - asyncio.get_event_loop().time())))
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

        self._flush()
        logger.info(self.stats.summary())
        return self.stats

    def _open(self):
        if self._connect is not None:
            return self._connect()
        import websockets

        return websockets.connect(WS_URL, additional_headers=self._headers())


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
