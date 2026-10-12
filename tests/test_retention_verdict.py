"""When retention goes red (2026-10-12 ruling: warn 75%, fail 90% of the cap).

The 500 MB "budget" was the old 0.5 GB tier carried over. At 807 MB (81% of
the real 1000 MB cap) with every step working, retention exited 1 every day,
and red that means nothing trains everyone to ignore red. Meanwhile the one
step that truly failed (compaction, every run since August) never turned the
run red at all.

Red now means exactly one of two things: the file is at 90% of Neon's cap,
or a retention step failed.
"""
from __future__ import annotations

import pytest

import src.maintenance.retention as retention
import src.maintenance.tape as tape
from src.maintenance.retention import CAP_FAIL_FRACTION, CAP_WARN_FRACTION, NEON_CAP_BYTES, RetentionPlan, format_size_line
from tests.test_maintenance_entrypoint import seeded  # noqa: F401
from tests.test_vacuum_full import _run_prune

MB = 1_000_000


def test_the_thresholds_are_the_ruled_ones():
    assert (CAP_WARN_FRACTION, CAP_FAIL_FRACTION) == (0.75, 0.90)


def test_a_healthy_run_at_81_percent_is_green(seeded, monkeypatch):
    """The 2026-10-12 run: 807 MB, every step fine. It exited 1."""
    assert _run_prune(seeded, monkeypatch, 807 * MB) == 0


def test_at_the_fail_line_it_is_red(seeded, monkeypatch):
    assert _run_prune(seeded, monkeypatch, int(0.91 * NEON_CAP_BYTES)) == 1


def test_a_failed_compaction_is_red_at_any_size(seeded, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("NotNullViolation")

    monkeypatch.setattr(tape, "compact_tape", broken)
    assert _run_prune(seeded, monkeypatch, 300 * MB) == 1


@pytest.mark.parametrize("mb,mark", [(700, "💾"), (760, "⚠️"), (860, "🚨")])
def test_the_digest_marks_the_file_against_the_cap(mb, mark):
    assert format_size_line(RetentionPlan(size_bytes=mb * MB, live_bytes=300 * MB)).startswith(mark)
