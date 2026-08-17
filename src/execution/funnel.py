"""Where qualifying opportunities go, counted at every gate they can die at.

The scoring funnel exists because "9 scored" could not be interrogated. The
execution stage had exactly the same hole one layer down: six opportunities
qualified, one traded, and the reason the other five did not was written to
`logger.info` and thrown away. A rejection reason that only reaches a log is a
rejection reason nobody reads.

The partition, asserted like the scoring one:

    qualifying == risk_rejected + sizing_zero + execution_returned_nothing
                + placed

Alert delivery is counted here too. `_send` swallows every exception into a
warning, so a trade that fires no Telegram message and a trade that fires one
look identical from outside — which is precisely the question that could not be
answered about trade 1/50.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class ExecutionFunnel:
    """Attribution for every qualifying opportunity in one cycle."""

    qualifying: int = 0

    # Terminal buckets, mutually exclusive and exhaustive.
    risk_rejected: int = 0
    execution_returned_nothing: int = 0
    placed: int = 0

    # Why risk said no. The reasons are the limit messages themselves, so a
    # cluster cap and a drawdown breaker are never the same line.
    rejection_reasons: Counter = field(default_factory=Counter)

    # Why execution returned nothing after risk approved. "execution returned
    # none 1" was the only line this funnel could produce for the single
    # qualifying opportunity in a cycle, which is exactly the uninterrogable
    # number the funnels exist to abolish.
    execution_reasons: Counter = field(default_factory=Counter)

    # What the shadow maker simulator did, per outcome. Reported per CYCLE and
    # not only in the daily digest: the first cycles after the flag was set
    # showed nothing at all, so "the simulator ran and refused" and "the flag
    # never reached the job" were indistinguishable. Those are completely
    # different problems.
    shadow_outcomes: Counter = field(default_factory=Counter)

    # Alert delivery, separately from trade placement. These are different
    # failures and conflating them is how "did the alert fire?" became
    # unanswerable.
    alerts_attempted: int = 0
    alerts_delivered: int = 0

    def record_rejection(self, reasons) -> None:
        self.risk_rejected += 1
        for reason in reasons or ["unspecified"]:
            # Limit messages embed live dollar amounts, so the varying part is
            # trimmed off — otherwise every rejection is its own unique reason
            # and the counter never aggregates.
            self.rejection_reasons[_reason_key(reason)] += 1

    def record_shadow(self, status: str) -> None:
        self.shadow_outcomes[status or "unknown"] += 1

    def record_execution_nothing(self, reason=None) -> None:
        self.execution_returned_nothing += 1
        self.execution_reasons[reason or "unspecified"] += 1

    def attributed(self) -> int:
        return (
            self.risk_rejected
            + self.execution_returned_nothing
            + self.placed
        )

    def balances(self) -> bool:
        return self.attributed() == self.qualifying

    def format(self) -> str:
        lines = [
            f"qualifying                  {self.qualifying:>6}",
            f"  - risk rejected           {self.risk_rejected:>6}",
            f"  - execution returned none {self.execution_returned_nothing:>6}",
            f"  = PLACED                  {self.placed:>6}",
        ]
        if not self.balances():
            lines.append(
                f"  !! UNATTRIBUTED           {self.qualifying - self.attributed():>6}"
                "   (a rejection path is uncounted)"
            )
        for reason, n in self.rejection_reasons.most_common():
            lines.append(f"    {reason}: {n}")
        for reason, n in self.execution_reasons.most_common():
            lines.append(f"    {reason}: {n}")
        if self.shadow_outcomes:
            detail = ", ".join(
                f"{k}={v}" for k, v in self.shadow_outcomes.most_common()
            )
            lines.append(f"shadow maker (simulation only, no orders): {detail}")
        if self.alerts_attempted or self.placed:
            undelivered = self.alerts_attempted - self.alerts_delivered
            lines.append(
                f"trade alerts: {self.alerts_delivered}/{self.alerts_attempted} delivered"
                + ("  !! UNDELIVERED" if undelivered else "")
            )
        return "\n".join(lines)

    def headline(self) -> str:
        return (
            f"{self.placed}/{self.qualifying} placed "
            f"(risk {self.risk_rejected}, exec {self.execution_returned_nothing}"
            + (
                f" [{self.execution_reasons.most_common(1)[0][0]}]"
                if self.execution_reasons else ""
            )
            + f", alerts {self.alerts_delivered}/{self.alerts_attempted})"
        )


def _reason_key(reason: str) -> str:
    """Collapse a limit message to its stable prefix.

    "Cluster exposure $12.40 exceeds max $10.00 ... for cluster 26AUG13" and
    the same message with different dollars are one reason, not two.
    """
    text = str(reason)
    for marker in (" $", " exceeds", " —"):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return text.strip() or "unspecified"
