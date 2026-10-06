"""The daily market session: recorder and trade loop in one job, 15:00–21:00 UTC.

    python -m src.session

Why one job and not two crons. GitHub's scheduler has been delivering ~5–8
runs a day against the 96 and 24 asked for, starting them 4–8 hours late
(measured 2026-10-06). The cadence was whatever GitHub felt like, and the
recorder's coverage was random slots. A session sets its own cadence once it
is running: a cycle every 15 minutes, the recorder continuous.

Why 15–21 UTC. Public prints on our 19 weather series put 53% of mid-priced
(10–90c) volume in those six hours, against 25% for an even spread. Ruling
2026-10-06; widen to 14–22 only after a week of console headroom.

Getting it started at all. A link is started by whichever of several cron
triggers GitHub delivers first. A link may live at most LINK_SECONDS (hosted
jobs die at 6 h). If it cannot cover the rest of the window it dispatches its
own successor through workflow_dispatch, which GITHUB_TOKEN is allowed to
trigger, unlike every other event. Duplicates queue behind the running link
in one concurrency group and exit when they see the window has closed.

Each cycle is `python -m src.run_trading` in a fresh process, exactly what the
15-minute workflow ran, with the same environment. Nothing about a cycle
changes: mode, limits, sizing and the live gate are untouched. The recorder
is `python -m src.recorder` in hourly segments, so its subscribe list is
rebuilt every hour instead of being frozen at start.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SESSION_START = dt.time(15, 0)
SESSION_END = dt.time(21, 0)
# Hosted jobs are killed at 6 h. Setup, install and shutdown fit in the rest.
LINK_SECONDS = 5 * 3600 + 40 * 60
# The cadence is configuration, not a second constant that agreed once.
from src.trading_config import CYCLE_MINUTES  # noqa: E402

CYCLE_SECONDS = CYCLE_MINUTES * 60
RECORDER_SEGMENT_SECONDS = 3600
# Hard ceiling on one cycle, matching the old workflow's 8-minute timeout.
CYCLE_TIMEOUT_SECONDS = 8 * 60
POLL_SECONDS = 10


@dataclass
class LinkPlan:
    action: str                          # "run" | "chain_now" | "exit"
    wait_until: Optional[dt.datetime] = None
    run_until: Optional[dt.datetime] = None
    chain: bool = False
    reason: str = ""


def _at(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, tzinfo=dt.timezone.utc)


def plan_link(now: dt.datetime, started: dt.datetime) -> LinkPlan:
    """What this link should do, from the clock alone."""
    open_, close = _at(now.date(), SESSION_START), _at(now.date(), SESSION_END)
    if now >= close:
        return LinkPlan("exit", reason=f"session closed at {close:%H:%M}Z")

    hard_stop = started + dt.timedelta(seconds=LINK_SECONDS)
    begin = max(now, open_)
    if begin >= hard_stop:
        return LinkPlan(
            "chain_now",
            reason=f"started {started:%H:%M}Z, too early to reach the {open_:%H:%M}Z open",
        )

    run_until = min(close, hard_stop)
    return LinkPlan(
        "run",
        wait_until=open_ if now < open_ else None,
        run_until=run_until,
        chain=run_until < close,
        reason=f"runs {begin:%H:%M}–{run_until:%H:%M}Z"
        + (" then hands off" if run_until < close else ""),
    )


def _next_grid(t: dt.datetime) -> dt.datetime:
    """The first point on the day's 15-minute grid (anchored at the open) at
    or after `t`. One grid for every link, so a hand-off neither doubles a
    cycle nor skips one."""
    open_ = _at(t.date(), SESSION_START)
    if t <= open_:
        return open_
    steps = -(-(t - open_).total_seconds() // CYCLE_SECONDS)
    return open_ + dt.timedelta(seconds=steps * CYCLE_SECONDS)


def run_session(
    now: Callable[[], dt.datetime],
    sleep: Callable[[float], None],
    run_cycle: Callable[[], int],
    start_recorder: Callable[[float], object],
    recorder_done: Callable[[], bool],
    dispatch_successor: Callable[[], bool],
    alert: Callable[[str], None],
) -> int:
    """Drive one link. Returns non-zero if anything failed, so the workflow
    run is red, but never stops the session early for a single failed cycle:
    the alert goes out at once and the next cycle still runs."""
    started = now()
    plan = plan_link(started, started)
    logger.info("Session link: %s (%s)", plan.action, plan.reason)

    if plan.action == "exit":
        return 0
    if plan.action == "chain_now":
        if dispatch_successor():
            return 0
        alert("🚨 Session could not dispatch its successor — no session today unless a trigger lands")
        return 1

    if plan.wait_until is not None:
        sleep(max(0.0, (plan.wait_until - now()).total_seconds()))

    failed = False
    cycle_alerted = False
    next_cycle = _next_grid(now())
    recorder_started = False

    while now() < plan.run_until:
        remaining = (plan.run_until - now()).total_seconds()

        if (not recorder_started or recorder_done()) and remaining > 120:
            start_recorder(min(RECORDER_SEGMENT_SECONDS, remaining - 60))
            recorder_started = True

        if now() >= next_cycle:
            code = run_cycle()
            if code != 0:
                failed = True
                if not cycle_alerted:
                    alert(f"🚨 Session cycle failed (exit {code}) — the session continues; see the run log")
                    cycle_alerted = True
            # The day's fixed grid, not "15 min after this one ended", so
            # slow cycles do not drift the cadence.
            next_cycle = _next_grid(now() + dt.timedelta(seconds=1))

        sleep(min(POLL_SECONDS, max(0.0, (plan.run_until - now()).total_seconds())))

    if plan.chain and not dispatch_successor():
        alert("🚨 Session could not dispatch its successor — the rest of today's window will not run")
        failed = True
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Production wiring
# --------------------------------------------------------------------------

def _dispatch_successor() -> bool:
    """POST a workflow_dispatch for this same workflow, on the same ref."""
    import httpx

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    workflow = os.environ.get("SESSION_WORKFLOW", "session.yml")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if not (repo and token):
        logger.error("Cannot dispatch successor: GITHUB_REPOSITORY/GITHUB_TOKEN unset")
        return False
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            json={"ref": ref},
            timeout=30,
        )
    except httpx.HTTPError:
        logger.exception("Successor dispatch failed")
        return False
    if response.status_code != 204:
        logger.error("Successor dispatch refused: %s %s", response.status_code, response.text[:300])
        return False
    logger.info("Successor dispatched")
    return True


def _alert(message: str) -> None:
    from src.alerts import Alerter

    run_url = os.environ.get("RUN_URL", "")
    if not Alerter().send(f"{message}\n{run_url}".strip()):
        logger.error("Alert NOT delivered: %s", message)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    recorder: dict = {"proc": None}

    def run_cycle() -> int:
        try:
            return subprocess.run(
                [sys.executable, "-m", "src.run_trading"], timeout=CYCLE_TIMEOUT_SECONDS,
            ).returncode
        except subprocess.TimeoutExpired:
            logger.error("Cycle exceeded %ds and was killed", CYCLE_TIMEOUT_SECONDS)
            return 124

    def start_recorder(seconds: float) -> None:
        recorder["proc"] = subprocess.Popen(
            [sys.executable, "-m", "src.recorder", "--duration", str(int(seconds))]
        )

    def recorder_done() -> bool:
        proc = recorder["proc"]
        if proc is None or proc.poll() is None:
            return False
        if proc.returncode != 0:
            logger.error("Recorder segment exited %s", proc.returncode)
        return True

    code = run_session(
        now=lambda: dt.datetime.now(dt.timezone.utc),
        sleep=time.sleep,
        run_cycle=run_cycle,
        start_recorder=start_recorder,
        recorder_done=recorder_done,
        dispatch_successor=_dispatch_successor,
        alert=_alert,
    )
    proc = recorder["proc"]
    if proc is not None and proc.poll() is None:
        proc.wait(timeout=RECORDER_SEGMENT_SECONDS)
    return code


if __name__ == "__main__":
    sys.exit(main())
