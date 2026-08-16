"""Where the market universe goes, counted at every gate it can die at.

"9 scored" is not interrogable. It is the same number whether the scorable set
is genuinely nine markets, or five thousand markets that all failed one
misfiring gate. Twice now the scorable set has been starved for a different
reason and both times the headline looked identical, so the count of survivors
is recorded alongside the count of casualties at each stage.

The invariant that makes it a funnel rather than a pile of counters:

    open_markets == stale_or_missing_snapshot + no_last_price
                  + prescreen_rejected + no_model_estimate
                  + price_derived_gated + budget_skipped + scored

Every market is attributed to exactly one bucket. If the arithmetic does not
close, a rejection path exists that nobody is counting — which is the failure
this module exists to make impossible.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ScoreFunnel:
    """Attribution for every open market in one scoring pass."""

    open_markets: int = 0
    with_fresh_snapshot: int = 0

    # Terminal buckets, mutually exclusive and exhaustive.
    no_last_price: int = 0
    prescreen_rejected: int = 0
    no_model_estimate: int = 0
    price_derived_gated: int = 0
    budget_skipped: int = 0
    scored: int = 0

    # Why "no model estimate" happened. attempts counts dispatches, estimates
    # counts non-None returns; a model with attempts and no estimates is either
    # dark or matching nothing, and those are different bugs.
    model_attempts: Counter = field(default_factory=Counter)
    model_estimates: Counter = field(default_factory=Counter)
    # Markets whose category no independent model claims at all. These can
    # never score while price-derived models are gated off, so they are the
    # structural size of the gap rather than a failure.
    no_independent_model: int = 0
    # Per-model refusal reasons, for models that report them.
    model_refusals: Dict[str, Counter] = field(default_factory=dict)
    # Markets the ingest refused to persist, per series. Outside the funnel's
    # balance (they never became rows, so they were never open markets), but
    # reported alongside it: a filter nobody can see is how a legitimate series
    # gets dropped for a month without anyone noticing.
    ingest_excluded: Dict[str, int] = field(default_factory=dict)
    # State of the metered odds feed. "SportsOddsModel produced nothing" is a
    # market fact if the feed is healthy and a source outage if it is not, and
    # the two must never be reported as the same line.
    odds_state: Dict[str, Any] = field(default_factory=dict)

    @property
    def stale_or_missing_snapshot(self) -> int:
        return max(0, self.open_markets - self.with_fresh_snapshot)

    def attributed(self) -> int:
        return (
            self.stale_or_missing_snapshot
            + self.no_last_price
            + self.prescreen_rejected
            + self.no_model_estimate
            + self.price_derived_gated
            + self.budget_skipped
            + self.scored
        )

    def balances(self) -> bool:
        """Does every open market land in exactly one bucket?"""
        return self.attributed() == self.open_markets

    def record_refusals(self, model_name: str, refusals) -> None:
        if not refusals:
            return
        self.model_refusals.setdefault(model_name, Counter()).update(refusals)

    def format(self) -> str:
        """Plain-text block for the Actions run summary and the digest."""
        lines = [
            f"open markets                {self.open_markets:>6}",
            f"  - stale/no snapshot       {self.stale_or_missing_snapshot:>6}",
            f"  - no last price           {self.no_last_price:>6}",
            f"  - prescreen rejected      {self.prescreen_rejected:>6}",
            f"  - no model estimate       {self.no_model_estimate:>6}"
            f"   (no independent model for category: {self.no_independent_model})",
            f"  - price-derived gated     {self.price_derived_gated:>6}",
            f"  - budget skipped          {self.budget_skipped:>6}",
            f"  = SCORED                  {self.scored:>6}",
        ]
        if not self.balances():
            lines.append(
                f"  !! UNATTRIBUTED           {self.open_markets - self.attributed():>6}"
                "   (a rejection path is uncounted)"
            )
        names = sorted(set(self.model_attempts) | set(self.model_estimates))
        if names:
            lines.append("models (dispatched -> estimated):")
            for name in names:
                lines.append(
                    f"  {name:<24} {self.model_attempts.get(name, 0):>6}"
                    f" -> {self.model_estimates.get(name, 0)}"
                )
        if self.ingest_excluded:
            total = sum(self.ingest_excluded.values())
            detail = ", ".join(
                f"{k}={v}" for k, v in sorted(
                    self.ingest_excluded.items(), key=lambda kv: -kv[1]
                )
            )
            lines.append(
                f"ingest excluded {total} markets before write: {detail}"
            )
        if self.odds_state:
            lines.append(
                "odds feed: "
                + ", ".join(f"{k}={v}" for k, v in sorted(self.odds_state.items()))
            )
        for name, reasons in sorted(self.model_refusals.items()):
            if not reasons:
                continue
            detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
            lines.append(f"  {name} refusals: {detail}")
        return "\n".join(lines)

    def headline(self) -> str:
        """One line, for the digest where a block does not fit."""
        return (
            f"{self.scored}/{self.open_markets} scored "
            f"(snapshot {self.stale_or_missing_snapshot}, "
            f"price {self.no_last_price}, "
            f"prescreen {self.prescreen_rejected}, "
            f"no-model {self.no_model_estimate}, "
            f"gated {self.price_derived_gated}, "
            f"budget {self.budget_skipped})"
            + (
                f" | ingest excluded {sum(self.ingest_excluded.values())}"
                if self.ingest_excluded else ""
            )
        )
