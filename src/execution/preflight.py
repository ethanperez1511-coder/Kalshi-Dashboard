"""What must be true before MAKER_ENABLED can mean anything.

A config flag is not a decision. Flipping `MAKER_ENABLED` while the legacy
integer money path is still live would route fractional fills through columns
that silently truncate them, so the flag alone must not be sufficient — the
checklist is enforced in code and the engine refuses to start rather than
trading on a half-migrated path.

Checks are automatic wherever the answer is discoverable from the system
itself. Two are attestations a human must make, and they are labelled as such:
an attestation that nobody has made reads as a blocker, never as a pass.

Ordered by what breaks worst if ignored.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from sqlalchemy import Engine, inspect

logger = logging.getLogger(__name__)


class MakerBlocked(RuntimeError):
    """MAKER_ENABLED is set but the checklist is not satisfied."""


@dataclass(frozen=True)
class CheckResult:
    item: str
    passed: bool
    detail: str
    automatic: bool


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

def check_decimal_migration(engine: Engine) -> Tuple[bool, str]:
    """Are quantity and money columns Numeric rather than Integer?

    Discoverable, so it is checked rather than attested. `count_fp` is
    fractional — 1541 of 2299 observed prints were non-integer — and an Integer
    column does not reject a fractional fill, it truncates it. A 0.4-contract
    fill becomes 0, an exposure check passes that should not have, and nothing
    anywhere reports an error.
    """
    inspector = inspect(engine)
    offenders: List[str] = []
    for table, column in (
        ("trades", "quantity"),
        ("positions", "quantity"),
    ):
        try:
            columns = {c["name"]: c for c in inspector.get_columns(table)}
        except Exception:
            return False, f"cannot inspect {table}"
        found = columns.get(column)
        if found is None:
            return False, f"{table}.{column} missing"
        if "INT" in str(found["type"]).upper():
            offenders.append(f"{table}.{column} is {found['type']}")

    if offenders:
        return False, (
            "legacy integer money path still live: " + ", ".join(offenders)
            + " — fractional fills would be truncated, not rejected"
        )
    return True, "quantity columns are Numeric"


def check_simulator_validated(engine: Engine) -> Tuple[bool, str]:
    """Has N actually been derived from recorded data?

    The design refused to assert N because it depends on trade-through
    frequency in the markets we quote, which is only measurable after the
    recorder runs. So the gate is evidence, not a date.
    """
    from src.execution.shadow import report_by_category
    from src.recorder.health import recorder_health

    health = recorder_health(engine)
    if not health["messages"]:
        return False, "no recorded book data — the N clock has not started"

    reports = [r for r in report_by_category(engine) if r.recognised_fills > 0]
    if not reports:
        return False, (
            f"{health['messages']} messages recorded but no recognised maker "
            f"fills yet — nothing to validate capture against"
        )
    return True, (
        "recognised fills in: "
        + ", ".join(f"{r.category} ({r.recognised_fills})" for r in reports)
    )


def check_capture_beats_taker(engine: Engine) -> Tuple[bool, str]:
    """Does measured capture actually exceed the taker path, per category?

    This is the phase's entire justification. Per category, never pooled — a
    liquid category must not carry an illiquid one over the line.
    """
    from src.execution.shadow import report_by_category

    reports = [r for r in report_by_category(engine) if r.recognised_fills > 0]
    if not reports:
        return False, "no recognised fills to measure capture on"

    losing = [
        f"{r.category} {r.mean_capture_cents:+.2f}c"
        for r in reports
        if (r.mean_capture_cents or 0) <= 0
    ]
    if losing:
        return False, "capture does not beat taker in: " + ", ".join(losing)
    return True, ", ".join(
        f"{r.category} {r.mean_capture_cents:+.2f}c" for r in reports
    )


def check_forecast_licensing(engine: Engine) -> Tuple[bool, str]:
    """Attestation. Open-Meteo's free tier is CC-BY-4.0 NON-COMMERCIAL.

    Not discoverable from the system, so a human states it. Unstated reads as
    blocked: the whole point is that nobody can flip a flag and have the
    licence question quietly resolve itself.
    """
    from src.trading_config import FORECAST_LICENSING_RESOLVED

    if FORECAST_LICENSING_RESOLVED:
        return True, "attested resolved (TRADING_FORECAST_LICENSING_RESOLVED)"
    return False, (
        "unattested — Open-Meteo free tier is non-commercial and forecast data "
        "sourcing must be settled before real capital is behind it"
    )


def check_live_gate_untouched(engine: Engine) -> Tuple[bool, str]:
    """Maker execution must not have become a way around the paper gate."""
    from src.models.settings import TradingSettings
    from src.database import get_session

    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        if settings is None:
            return False, "no trading settings row"
        if settings.paper_trades_before_live < 50:
            return False, (
                f"paper gate weakened to {settings.paper_trades_before_live} "
                f"— maker execution may not lower it"
            )
    return True, "paper gate intact at 50"


CHECKS: List[Tuple[str, Callable[[Engine], Tuple[bool, str]], bool]] = [
    ("decimal_migration", check_decimal_migration, True),
    ("forecast_licensing", check_forecast_licensing, False),
    ("simulator_validated", check_simulator_validated, True),
    ("capture_beats_taker", check_capture_beats_taker, True),
    ("live_gate_untouched", check_live_gate_untouched, True),
]


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def run_checklist(engine: Engine) -> List[CheckResult]:
    results: List[CheckResult] = []
    for item, check, automatic in CHECKS:
        try:
            passed, detail = check(engine)
        except Exception as exc:
            # A check that errored has not passed. Treating an exception as a
            # pass is the failure mode this whole file exists to prevent.
            passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
        results.append(CheckResult(item, passed, detail, automatic))
    return results


def maker_blockers(engine: Engine) -> List[str]:
    return [f"{r.item}: {r.detail}" for r in run_checklist(engine) if not r.passed]


def assert_maker_allowed(engine: Engine, maker_enabled: Optional[bool] = None) -> None:
    """Refuse to run maker execution unless every blocking item is satisfied.

    Called on the execution path, not at import, so that a misconfiguration is
    a loud failure at the moment it would have traded rather than a silent
    module-level import error somewhere unrelated.
    """
    if maker_enabled is None:
        from src.trading_config import MAKER_ENABLED

        maker_enabled = MAKER_ENABLED
    if not maker_enabled:
        return

    blockers = maker_blockers(engine)
    if blockers:
        raise MakerBlocked(
            "MAKER_ENABLED is set but the maker-enable checklist is not "
            "satisfied. Refusing to place maker orders.\n  - "
            + "\n  - ".join(blockers)
        )


def format_checklist(results: List[CheckResult]) -> str:
    lines = ["Maker-enable checklist:"]
    for result in results:
        mark = "✅" if result.passed else "❌"
        kind = "auto" if result.automatic else "attestation"
        lines.append(f"  {mark} [{kind}] {result.item}: {result.detail}")
    return "\n".join(lines)
