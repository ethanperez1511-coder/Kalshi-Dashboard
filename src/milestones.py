"""Paper-trade milestones, announced once each; the live gate once, ever.

Until 2026-10-12 these were checked against the count on every cycle, so the
gate message repeated every 15 minutes from the 50th trade on, and "40/50"
repeated for as long as the count sat at 40. The highest milestone already
announced is now stored on the settings row.

Announcement only: nothing here reads or writes `mode`. Reaching the gate
flips nothing; a human does.
"""
from __future__ import annotations

from sqlalchemy import Engine

from src.database import get_session
from src.models.settings import TradingSettings

MILESTONES = (10, 20, 25, 30, 40)


def announce_milestones(engine: Engine, alerter, paper_count: int, gate: int) -> None:
    with get_session(engine) as session:
        row = session.query(TradingSettings).first()
        if row is None:
            return
        last = row.last_milestone_alerted or 0
        reached = [m for m in MILESTONES if last < m <= paper_count and m < gate]
        if paper_count >= gate > last:
            alerter.live_gate_reached()
            row.last_milestone_alerted = gate
        elif reached:
            alerter.milestone(reached[-1], gate)
            row.last_milestone_alerted = reached[-1]
        else:
            return
        session.commit()
