"""A read-only dispatch that produces no report is a FAILED dispatch.

The day-7 job was dispatched, exited 0, and showed a green check having written
nothing to the run summary and two lines to the log. It had "succeeded". The
operator got no report, and nothing anywhere said so.

Two independent defects made that possible and both are the same mistake:

  `day7.main` printed to stdout and never wrote the run summary, unlike every
  other dispatchable action. So the summary was empty by construction, not by
  circumstance.

  `format_report` emits its header whether or not there are any categories, so
  a result set with nothing in it renders as a title with no body — visually
  indistinguishable from a healthy run at a glance, and exit 0 either way.

This module makes the empty case loud, once, for every read-only dispatch, so
it cannot be re-decided per action. A diagnostic that succeeds invisibly is not
a diagnostic; it is a green check that means nothing, which is worse than a red
one because it ends the investigation.
"""
from __future__ import annotations

import logging
import sys

from src.run_summary import write_summary

logger = logging.getLogger(__name__)


def publish_report(headline: str, body: str, substantive: bool) -> int:
    """Print, write the run summary, and return the exit code.

    `substantive` is the caller's answer to "did this actually measure
    anything?" — it is deliberately not inferred from the text, because a
    report that renders a header for an empty result set is exactly the failure
    this exists to catch, and inferring emptiness from the string would be
    fooled by the same header.

    Returns 0 when there is a report, 1 when there is not.
    """
    print(body)

    if substantive:
        write_summary(headline, body[:4000], ok=True)
        return 0

    message = (
        f"{headline} — PRODUCED NO REPORT.\n\n"
        "The job ran to completion and measured nothing. That is a failure, "
        "not a success: a read-only diagnostic exists to answer a question, "
        "and a green check with an empty body ends the investigation without "
        "answering it.\n\n"
        "What ran:\n" + (body or "(no output at all)")
    )
    logger.error("%s produced no report — failing the run deliberately", headline)
    write_summary(f"{headline}: NO REPORT", message[:4000], ok=False)
    print(message, file=sys.stderr)
    return 1
