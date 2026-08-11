"""Push a Telegram alert when the scheduled live-checks run fails.

The live suite exists to notice upstream contracts changing under us — a Kalshi
series repointed at a different settlement station, IEM dropping a station,
NCEI changing a field. None of that is visible from the local test suite.

But a scheduled guard that fails quietly in Actions is the same bug as a test
that skips itself, one level up: something is watching, and nothing is watched.
GitHub's own failure email is easy to filter into oblivion, so the failure gets
pushed to the same channel as trades and settlements.
"""
from __future__ import annotations

import os
import sys

from src.alerts import Alerter


def main() -> int:
    run_url = os.environ.get("RUN_URL", "(no run url)")
    alerter = Alerter()
    alerter.send(
        "🚨 <b>LIVE CHECKS FAILED</b>\n"
        "An upstream contract may have changed under us — settlement station, "
        "MOS availability, or contract terms.\n"
        "Weather pricing should be treated as suspect until this is read.\n"
        f"{run_url}"
    )
    # Never mask the original failure: this step runs only on failure, and the
    # job must stay red regardless of whether the notification got out.
    return 0


if __name__ == "__main__":
    sys.exit(main())
