"""Push a Telegram alert when the scheduled live-checks run fails.

The live suite exists to notice upstream contracts changing under us — a Kalshi
series repointed at a different settlement station, IEM dropping a station,
NCEI changing a field. None of that is visible from the local test suite.

But a scheduled guard that fails quietly in Actions is the same bug as a test
that skips itself, one level up: something is watching, and nothing is watched.
GitHub's own failure email is easy to filter into oblivion, so the failure gets
pushed to the same channel as trades and settlements.

One level further up again: this alerter itself failed quietly. live-checks
failed on 2026-08-14, 08-15 and 08-16 — all seven temperature series had been
repointed at a new settlement source — and no message arrived. The workflow
step was configured correctly. `send()`'s delivery bool was simply thrown away
and 0 returned regardless, so a refused send and a delivered one were the same
green step. The delivery result is now the exit code.
"""
from __future__ import annotations

import os
import sys

from src.alerts import Alerter
from src.run_summary import write_summary


def main() -> int:
    run_url = os.environ.get("RUN_URL", "(no run url)")
    alerter = Alerter()
    delivered = alerter.send(
        "🚨 <b>LIVE CHECKS FAILED</b>\n"
        "An upstream contract may have changed under us — settlement station, "
        "MOS availability, or contract terms.\n"
        "Weather pricing should be treated as suspect until this is read.\n"
        f"{run_url}"
    )

    if delivered:
        write_summary(f"Live-checks failure alert delivered to Telegram\n\n{run_url}")
        # The original failure stands on its own: this step runs only under
        # `if: failure()`, so the job is already red and nothing is masked.
        return 0

    # The run summary is the fallback channel when the primary one is gone, and
    # a red step is what makes "alerted" distinguishable from "alerted nobody"
    # on the Actions page months later.
    text = (
        "LIVE CHECKS FAILED and the Telegram alert did NOT go out — Telegram "
        "refused the message, or TELEGRAM_TOKEN / TELEGRAM_CHAT_ID are not set "
        "on this workflow. Read this run: nothing reached a phone.\n\n"
        f"{run_url}"
    )
    write_summary("Live-checks alert NOT delivered", text, ok=False)
    print(text, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
