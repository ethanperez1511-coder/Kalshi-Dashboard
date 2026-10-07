"""Start today's market session from a backstop cycle, if nothing else has.

    python -m src.session_kick

market-session's five daily cron triggers did not fire on its first two days,
while the */15 backstop was delivered several times a day. So a backstop run
that lands between KICK_FROM and the last link's start, with no session link
running or queued, dispatches one through workflow_dispatch with GITHUB_TOKEN.
A link that crashed mid-afternoon gets restarted the same way.

Best effort and never fatal: exits 0 whatever happens, so it can never cost
the backstop its cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import List

from src.session import SESSION_END, _dispatch_successor
from src.session_watchdog import session_alive

logger = logging.getLogger(__name__)

# Earliest a kick is useful (a link started earlier only sleeps to the open,
# and one started before 09:20 cannot reach it), and the latest: after 20:40
# the tail link alone is left, and the backstop covers those 20 minutes.
KICK_FROM = dt.time(13, 0)
KICK_UNTIL = dt.time(20, 40)


def should_kick(now: dt.datetime, runs: List[dict]) -> bool:
    start = dt.datetime.combine(now.date(), KICK_FROM, tzinfo=dt.timezone.utc)
    until = dt.datetime.combine(now.date(), KICK_UNTIL, tzinfo=dt.timezone.utc)
    assert until.time() < SESSION_END
    return start <= now < until and not session_alive(runs)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        import httpx

        repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
        now = dt.datetime.now(dt.timezone.utc)
        runs = httpx.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/session.yml/runs",
            params={"created": f">={now.date().isoformat()}", "per_page": 50},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=20,
        ).json().get("workflow_runs", [])
        if should_kick(now, runs):
            ok = _dispatch_successor()
            logger.warning("No session link running at %s — dispatched one: %s",
                           now.strftime("%H:%M"), "ok" if ok else "FAILED")
        else:
            logger.info("Session kick: not needed at %s (%d runs today)",
                        now.strftime("%H:%M"), len(runs))
    except Exception:
        logger.warning("Session kick failed (non-fatal)", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
