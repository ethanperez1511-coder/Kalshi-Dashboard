"""Milestones are announced once each, and the gate exactly once, ever.

"🚀 50 paper trades complete!" was sent on every cycle once the count reached
50, and "🎯 Paper trades: 40/50" on every cycle while the count sat at 40. A
message that repeats every 15 minutes stops being read, and the one that
matters arrives buried.
"""
from __future__ import annotations

import pytest

from src.database import Base, get_session
from src.milestones import announce_milestones
from src.models.settings import TradingSettings


class _Alerter:
    def __init__(self):
        self.milestones, self.gates = [], 0

    def milestone(self, count, total):
        self.milestones.append(count)

    def live_gate_reached(self):
        self.gates += 1


@pytest.fixture
def engine(db_engine):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings(bankroll=100.0, paper_trade_count=0))
        s.commit()
    return db_engine


def _cycles(engine, alerter, counts, gate=50):
    for c in counts:
        announce_milestones(engine, alerter, c, gate)


def test_the_gate_is_announced_once_however_many_cycles_follow(engine):
    a = _Alerter()
    _cycles(engine, a, [50, 50, 50, 51, 52, 60])
    assert a.gates == 1


def test_a_milestone_is_announced_once_while_the_count_sits_on_it(engine):
    a = _Alerter()
    _cycles(engine, a, [40, 40, 40, 41])
    assert a.milestones == [40]


def test_each_milestone_once_in_order(engine):
    a = _Alerter()
    _cycles(engine, a, [9, 10, 10, 19, 20, 25, 30, 30])
    assert a.milestones == [10, 20, 25, 30]


def test_a_jump_over_several_milestones_announces_the_highest(engine):
    a = _Alerter()
    _cycles(engine, a, [8, 33])
    assert a.milestones == [30]
