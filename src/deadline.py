"""Cooperative per-stage time budgets.

A cycle died twice on the GitHub 8-minute job cap: once on ~135k per-market
queries, once on an infinite retry loop in an external API. Both times the
platform timeout was the only thing that stopped it, which meant scoring,
settlement and the digest were all destroyed by one bad stage.

The job cap should be the last resort, not the mechanism. Each stage now gets
its own allotment; when it runs out the stage stops taking on new work, returns
what it has, and the cycle continues. That is a deliberate trade: a partial
ingest is worth far more than a cancelled tick, because ingest is idempotent and
the next cycle five minutes later picks up the rest.

Cooperative, not pre-emptive: Python cannot safely interrupt a running call, so
loops check the deadline between iterations. That bounds a stage to one
in-flight operation past its budget rather than to the budget exactly, which is
why every external call also carries its own timeout.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class Deadline:
    def __init__(self, seconds: Optional[float], name: str = "stage"):
        self.name = name
        self.seconds = seconds
        self._end = None if seconds is None else time.monotonic() + seconds
        self.exceeded = False

    def expired(self) -> bool:
        if self._end is None:
            return False
        if time.monotonic() >= self._end:
            if not self.exceeded:
                self.exceeded = True
                logger.warning(
                    "%s exceeded its %.0fs budget — stopping cleanly and letting "
                    "the rest of the cycle proceed", self.name, self.seconds,
                )
            return True
        return False

    def remaining(self) -> float:
        if self._end is None:
            return float("inf")
        return max(0.0, self._end - time.monotonic())

    @classmethod
    def none(cls, name: str = "stage") -> "Deadline":
        """An unbounded deadline, for tests and local runs."""
        return cls(None, name)
