"""Match decisions made by a human, committed to the repo.

The review queue lives in the database, and the database that matters is Neon —
which is only reachable from the cron runner. So a decision taken while reading
a report has no way into production unless it is checked in. These are applied
idempotently at the start of every pipeline cycle.

A seeded verdict never overrides a later decision made in the dashboard: it
fills an empty slot or upgrades a `pending` row, and otherwise leaves the row
alone. `decided_by` records which of the two it was.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from sqlalchemy import Engine

from src.database import get_session
from src.models.match_map import MarketMatchMap

logger = logging.getLogger(__name__)

SEED_SOURCE = "seed:phase-1.5"


@dataclass(frozen=True)
class SeededDecision:
    kalshi_market_id: str
    poly_condition_id: str
    status: str  # "approved" | "blocked"
    kalshi_title: str
    poly_question: str
    reason: str


DECISIONS: List[SeededDecision] = [
    SeededDecision(
        kalshi_market_id="KXNEXTISRAELPM-45JAN01-GEIS",
        poly_condition_id=(
            "0xdbe93b5a701f36076a560fa4b9ba59e365a6e8e2ea6a83764640010657277ca4"
        ),
        status="blocked",
        kalshi_title="Will Gadi Eisenkot be the next Prime Minister of Israel?",
        poly_question="Will Gadi Eizenkot be the next Prime Minister of Israel?",
        reason=(
            "Same person (Eisenkot/Eizenkot is a transliteration variant), but "
            "NOT the same question. Verified against the live Gamma API on "
            "2026-08-11, the Polymarket contract states: 'This market will "
            "resolve to the next individual who is officially appointed and "
            "sworn in as Prime Minister of Israel following the 2026 "
            "parliamentary election ... If no such Prime Minister is sworn in "
            "by December 31, 2027, 11:59 PM ET, this market will resolve to "
            "\"Other\".' The Kalshi contract has no election scoping and closes "
            "2045-01-01. So P(Polymarket YES) is bounded above by "
            "P(Kalshi YES), and the gap always points the same way — using the "
            "Polymarket price as p_model understates Kalshi and manufactures a "
            "persistent one-sided 'NO is cheap' edge that cannot be arbitraged "
            "and would not reveal itself until settlement, years out."
        ),
    ),
    SeededDecision(
        kalshi_market_id="KXTAYLORSWIFTWEDDINGATTEND-28DEC31-MAX",
        poly_condition_id=(
            "0x0d9d760ff17a0e64ff9b67f48893c0b1ae4874cd462ce6c2d38c82e2b9171fda"
        ),
        status="blocked",
        kalshi_title="Will Max Martin attend Taylor Swift and Travis Kelce's Wedding?",
        poly_question="Will Max Martin attend Taylor Swift's wedding?",
        reason=(
            "Contradictory resolution standards, verified against both live APIs "
            "on 2026-08-11. Kalshi counts attendance 'reported present by any "
            "Source Agency, including social media posts by the person "
            "themselves', and states 'virtual attendance counts' and 'brief "
            "appearances or partial attendance count'. Polymarket requires "
            "'only physical attendance ... virtual attendance or confirmation of "
            "an invitation will not count', evidenced by photo/video or a "
            "first-party statement. They have ALREADY diverged: Kalshi settled "
            "YES on 2026-07-05; Polymarket is still open and formally disputed "
            "at 68.4c. Deadlines differ too — Polymarket force-resolves NO if no "
            "wedding occurs by 2026-12-31, Kalshi runs to 2028-12-31."
        ),
    ),
]


def apply_seed_decisions(engine: Engine) -> List[str]:
    """Write the checked-in decisions into the match map. Returns what changed."""
    applied: List[str] = []
    with get_session(engine) as session:
        for decision in DECISIONS:
            row = (
                session.query(MarketMatchMap)
                .filter_by(kalshi_market_id=decision.kalshi_market_id)
                .first()
            )
            if row is not None and row.decided_by == "human":
                continue  # a dashboard decision outranks the checked-in one
            if row is not None and row.status == decision.status:
                continue  # already applied
            if row is None:
                row = MarketMatchMap(kalshi_market_id=decision.kalshi_market_id)
                session.add(row)

            row.poly_condition_id = decision.poly_condition_id
            row.status = decision.status
            row.similarity = 0.0
            row.kalshi_title = decision.kalshi_title
            row.poly_question = decision.poly_question
            row.verdict = "conflict" if decision.status == "blocked" else "match"
            row.reason = decision.reason
            row.decided_by = SEED_SOURCE
            row.decided_at = datetime.now(timezone.utc)
            applied.append(f"{decision.kalshi_market_id}={decision.status}")

        session.commit()

    if applied:
        logger.info("Applied seeded match decisions: %s", ", ".join(applied))
    return applied
