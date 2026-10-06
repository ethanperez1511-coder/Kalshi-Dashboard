"""Is today's market session running? And keep every workflow enabled.

    python -m src.session_watchdog

A session that never starts is silent: no cycle runs, so no cycle can fail and
no digest goes out. This asks GitHub, not the database, so it costs no Neon
compute. Inside the window, no session run in progress or queued means a
Telegram alert.

The same job re-enables every workflow. In a public repository GitHub disables
scheduled workflows after 60 days without repository activity, and a disabled
workflow cannot re-enable itself. Enabling an enabled workflow is a no-op and
resets that clock, so doing it daily is the cheap form of the monthly ruling.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import List, Optional

import httpx

from src.session import SESSION_END, SESSION_START

logger = logging.getLogger(__name__)
API = "https://api.github.com"
LIVE_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}


def session_alive(runs: List[dict]) -> bool:
    return any(r.get("status") in LIVE_STATES for r in runs)


def verdict(now: dt.datetime, runs: List[dict]) -> Optional[str]:
    """An alert message, or None if all is well."""
    open_ = dt.datetime.combine(now.date(), SESSION_START, tzinfo=dt.timezone.utc)
    close = dt.datetime.combine(now.date(), SESSION_END, tzinfo=dt.timezone.utc)
    if not (open_ <= now < close):
        return None
    if session_alive(runs):
        return None
    return (
        f"🚨 <b>NO MARKET SESSION RUNNING</b> at {now:%H:%M}Z — no trade cycles "
        f"and no recording until one starts. Dispatch the session workflow by hand."
    )


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API, timeout=30,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        logger.error("GITHUB_REPOSITORY/GITHUB_TOKEN unset")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    failed = False
    with _client(token) as gh:
        runs = gh.get(
            f"/repos/{repo}/actions/workflows/session.yml/runs",
            params={"created": f">={now.date().isoformat()}", "per_page": 50},
        ).json().get("workflow_runs", [])
        message = verdict(now, runs)
        if message:
            from src.alerts import Alerter

            failed = True
            if not Alerter().send(message):
                logger.error("Alert NOT delivered: %s", message)
        else:
            logger.info("Session check OK at %s (%d runs today)", now.isoformat(), len(runs))

        for wf in gh.get(f"/repos/{repo}/actions/workflows").json().get("workflows", []):
            r = gh.put(f"/repos/{repo}/actions/workflows/{wf['id']}/enable")
            if r.status_code != 204:
                logger.error("Could not enable %s: %s", wf["path"], r.status_code)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
