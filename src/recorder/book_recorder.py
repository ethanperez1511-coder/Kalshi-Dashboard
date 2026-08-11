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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import Engine, select

from src.database import get_session
from src.kalshi.auth import KalshiAuth
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


@dataclass
class RecorderStats:
    messages: int = 0
    written: int = 0
    gaps: int = 0
    reconnects: int = 0
    markets: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"book recorder: {self.written} messages written across "
            f"{len(self.markets)} markets, {self.gaps} sequence gaps, "
            f"{self.reconnects} reconnects"
        )


def markets_to_record(engine: Engine, limit: int = MAX_MARKETS) -> List[str]:
    """Markets worth recording: anything we hold, plus anything we are scoring.

    Open positions come first and are never dropped by the cap — a market we
    are actually exposed to matters more than one we are merely watching.
    """
    with get_session(engine) as session:
        held = [
            row[0] for row in session.execute(
                select(Position.market_id).where(Position.status == "open").distinct()
            ).all()
        ]
        scored = [
            row[0] for row in session.execute(
                select(Opportunity.market_id)
                .where(Opportunity.status.in_(("qualifying", "watching")))
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

    def _flush(self) -> None:
        if not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        with get_session(self._engine) as session:
            session.bulk_insert_mappings(OrderbookDeltaRaw, rows)
            session.commit()
        self.stats.written += len(rows)

    def _record_gap(self, ticker: str, sid: Optional[int], expected: int, got: int):
        with get_session(self._engine) as session:
            session.add(OrderbookGap(
                market_ticker=ticker, sid=sid, expected_seq=expected,
                received_seq=got, missing=max(0, got - expected),
            ))
            session.commit()
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
