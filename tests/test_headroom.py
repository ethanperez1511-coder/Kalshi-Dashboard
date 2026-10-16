"""Judge real headroom: live data against the cap, with reusable space counted.

2026-10-13: retention went red at 903 MB file / 90% of the cap while 206 MB
inside that file was reusable (~697 MB live). New writes fill reusable space
before the file grows, so the file overstated the risk by 206 MB. Worse, the
compaction cap guard (file >= 90%) then blocked the one step that frees
space, and the digest's "Neon cap in 4.2 days" extrapolated FILE growth while
live data was flat.

The physical file still matters: Neon refuses a write that must EXTEND the
file past the cap. So there is a hard line on the file too, set high enough
that it means the reusable space is nearly gone.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.maintenance.tape as tape
from src.maintenance.retention import (
    CAP_FAIL_FRACTION,
    FILE_HARD_FRACTION,
    NEON_CAP_BYTES,
    RetentionPlan,
    format_size_line,
)
from tests.test_maintenance_entrypoint import seeded  # noqa: F401
from tests.test_vacuum_full import _run_prune

MB = 1_000_000


class TestVerdict:
    def test_the_oct_13_state_is_not_red(self, seeded, monkeypatch):
        """903 MB file, ~697 MB live: 70% real use."""
        assert _run_prune(seeded, monkeypatch, 903 * MB, live_bytes=697 * MB) == 0

    def test_live_at_the_fail_line_is_red(self, seeded, monkeypatch):
        assert _run_prune(seeded, monkeypatch, 950 * MB, live_bytes=905 * MB) == 1

    def test_a_file_at_the_hard_line_is_red_whatever_is_live(self, seeded, monkeypatch):
        assert _run_prune(seeded, monkeypatch, int(FILE_HARD_FRACTION * NEON_CAP_BYTES) + MB,
                          live_bytes=500 * MB) == 1

    def test_without_a_live_measurement_the_file_is_judged(self, seeded, monkeypatch):
        assert _run_prune(seeded, monkeypatch, 905 * MB) == 1

    def test_the_lines(self):
        assert (CAP_FAIL_FRACTION, FILE_HARD_FRACTION) == (0.90, 0.97)


class TestDigest:
    def test_the_line_states_real_headroom(self):
        line = format_size_line(RetentionPlan(size_bytes=903 * MB, live_bytes=697 * MB))
        assert "headroom ~303 MB" in line
        assert line.startswith("⚠️")          # file >= 90%: worth a look, not an alarm

    def test_live_near_the_cap_is_the_alarm(self):
        assert format_size_line(RetentionPlan(size_bytes=900 * MB, live_bytes=860 * MB)).startswith("🚨")


class TestCompactionGuard:
    def test_compaction_runs_at_91_percent(self, engine_with_tape, monkeypatch):
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: int(0.91 * NEON_CAP_BYTES))
        result = tape.compact_tape(engine_with_tape, tape.plan_compaction(engine_with_tape, now=NOW), now=NOW)
        assert result["nulled"] == 6 and not result["stopped"]

    def test_it_runs_above_95_percent_too(self, engine_with_tape, monkeypatch):
        """Ruling 2026-10-16: the file never shrinks, so a 95% line blocked
        compaction permanently once crossed. File growth bounds it instead."""
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: int(1.05 * NEON_CAP_BYTES))
        result = tape.compact_tape(engine_with_tape, tape.plan_compaction(engine_with_tape, now=NOW), now=NOW)
        assert result["nulled"] == 6 and not result["stopped"]


class TestProjection:
    def test_flat_live_data_has_no_deadline_however_the_file_moved(self, db_engine):
        from src.database import Base
        from src.db_growth import format_growth, growth, record_sample
        import src.models  # noqa: F401

        Base.metadata.create_all(db_engine)
        today = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
        record_sample(db_engine, now=today - dt.timedelta(days=1), size_bytes=814 * MB, live_bytes=700 * MB)
        record_sample(db_engine, now=today, size_bytes=903 * MB, live_bytes=697 * MB)

        text = format_growth(growth(db_engine, now=today))
        assert "NEON CAP IN" not in text

    def test_growing_live_data_projects_from_live_headroom(self, db_engine):
        from src.database import Base
        from src.db_growth import growth, record_sample
        import src.models  # noqa: F401

        Base.metadata.create_all(db_engine)
        today = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
        record_sample(db_engine, now=today - dt.timedelta(days=2), size_bytes=903 * MB, live_bytes=600 * MB)
        record_sample(db_engine, now=today, size_bytes=903 * MB, live_bytes=700 * MB)

        result = growth(db_engine, now=today)
        assert result["days_to_full"] == pytest.approx(300 / 50, rel=0.01)   # 300 MB headroom / 50 MB a day


NOW = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine_with_tape(tmp_path):
    from src.database import Base, get_engine, get_session
    from src.models.orderbook_raw import OrderbookDeltaRaw

    engine = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    with get_session(engine) as s:
        for i in range(6):
            s.add(OrderbookDeltaRaw(market_ticker="M", msg_type="delta", sid=1, seq=i, payload="{}",
                                    received_at=NOW - dt.timedelta(days=3)))
        s.commit()
    return engine
