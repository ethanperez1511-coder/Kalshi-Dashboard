"""The live suite must exist, be marked, and actually run somewhere.

A guard that can quietly not run is not a guard. The station-mapping check was
originally written with `skipif(no credentials)`, which meant that on any
machine without secrets it reported success while verifying nothing — and that
is precisely the failure it was written to catch.

The arrangement now is: live tests carry the `live` marker, the default suite
deselects them, and a scheduled workflow runs them WITH credentials where
missing credentials fail hard. That only holds while all three pieces exist, so
this file asserts they do. It runs in the DEFAULT suite on purpose — it is the
tripwire that fires if someone deletes the live job or unmarks the tests.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"


def test_live_marker_is_registered():
    """An unregistered marker is a typo waiting to silently deselect nothing."""
    config = (REPO / "pyproject.toml").read_text()
    assert re.search(r'^\s*"live:', config, re.MULTILINE), (
        "the `live` marker is not declared in pyproject.toml"
    )


def test_default_run_excludes_live_tests():
    config = (REPO / "pyproject.toml").read_text()
    assert "not live" in config, (
        "the default suite no longer deselects live tests, so a laptop without "
        "credentials will fail or, worse, start skipping them again"
    )


def test_live_tests_actually_exist():
    """If the marked suite is empty, the scheduled job is verifying nothing."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "live", "--collect-only", "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    combined = result.stdout + result.stderr
    match = re.search(r"(\d+)\s+tests? collected", combined)
    if not match:
        match = re.search(r"(\d+)/\d+ tests collected", combined)
    assert match, f"could not determine live test count from:\n{combined[-800:]}"
    assert int(match.group(1)) > 0, "no tests carry the `live` marker"


def test_a_workflow_runs_the_live_suite():
    """The marked suite has to be wired to something that executes it."""
    assert WORKFLOWS.is_dir(), "no .github/workflows directory"
    runners = [
        path for path in WORKFLOWS.glob("*.yml")
        if re.search(r"-m\s+['\"]?live", path.read_text())
    ]
    assert runners, (
        "no workflow runs `pytest -m live` — the live tests would never execute "
        "anywhere, which is the same as deleting them"
    )


def test_the_live_workflow_supplies_credentials():
    """Running the live suite without secrets would fail every time, which
    trains everyone to ignore it."""
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text()
        if re.search(r"-m\s+['\"]?live", text):
            assert "KALSHI_API_KEY" in text, (
                f"{path.name} runs the live suite but passes no Kalshi credentials"
            )
            return
