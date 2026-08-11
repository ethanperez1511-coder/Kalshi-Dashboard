"""Read the terms of a daily temperature contract, or refuse to price it.

Two facts decide what one of these contracts asks, and neither may be assumed:

  direction  A single city lists both ">90°" and "<83°" contracts for the same
             day. Guessing inverts the question — the Polymarket direction bug
             from Phase 1.3, in a new place.
  threshold  The number, and the unit it is in.

Kalshi publishes both as structured fields (`strike_type` + `floor_strike` /
`cap_strike`), so those are the source of truth. The human-readable subtitle is
used as an independent cross-check, because it encodes the boundary convention
that the structured fields leave implicit:

    floor_strike = 90, strike_type = "greater", subtitle = "91° or above"

YES therefore means T >= 91 — strictly greater than the strike, not "at or
above" it. Temperatures settle as whole degrees from the NWS climatological
report, so a one-degree error here moves probability mass at exactly the
threshold most likely to be near the money.

When the structured fields are absent the title is parsed instead, and when
neither can be confirmed the contract is marked unpriceable. There is no
default: a contract we cannot read is one we do not trade.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Kalshi strike_type -> our direction. Deliberately does NOT include "between":
# a range contract is a different question, and flattening it to a one-sided
# threshold would invent a price.
_STRIKE_TYPES = {
    "greater": "above",
    "greater_or_equal": "above_inclusive",
    "less": "below",
    "less_or_equal": "below_inclusive",
}

# Types Kalshi publishes that we understand but do not yet model. These are a
# different situation from a contract we cannot read, and conflating the two
# makes the coverage metric lie: measured live on 2026-08-11, 56 of 84 daily
# temperature contracts are `between` buckets, and reporting them as
# "unreadable" would raise a parser alarm for a modelling gap.
UNSUPPORTED_STRIKE_TYPES = {"between"}

_TITLE_RE = re.compile(
    r"(?:be|is)\s*(?P<op>>=|<=|>|<|above|below|at least|at most)\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>°|degrees?|F\b)?",
    re.IGNORECASE,
)

_TITLE_OPS = {
    ">": "above", "above": "above",
    "<": "below", "below": "below",
    ">=": "above_inclusive", "at least": "above_inclusive",
    "<=": "below_inclusive", "at most": "below_inclusive",
}

# "91° or above" / "82° or below" / "98 or below"
_SUBTITLE_RE = re.compile(
    r"(?P<value>-?\d+(?:\.\d+)?)\s*°?\s*(?:F\b)?\s*or\s+(?P<word>above|below|higher|lower|more|less)",
    re.IGNORECASE,
)
_SUBTITLE_WORDS = {
    "above": "above", "higher": "above", "more": "above",
    "below": "below", "lower": "below", "less": "below",
}


@dataclass(frozen=True)
class ContractTerms:
    """What a temperature contract actually asks."""

    direction: str      # "above" | "below" (exclusive of the threshold)
    threshold: float
    unit: str           # "F"
    source: str         # "structured" | "title"

    def resolves_yes(self, observed: float) -> bool:
        """Would this contract settle YES at `observed` degrees?

        Both directions are strict: a contract reading ">90°" settles NO at
        exactly 90, and "<83°" settles NO at exactly 83. Confirmed against the
        live subtitles ("91° or above", "82° or below").
        """
        if self.direction == "above":
            return observed > self.threshold
        return observed < self.threshold


def _subtitle_terms(text: str) -> Optional[tuple]:
    """(direction, inclusive_bound) from a subtitle, or None if unreadable."""
    if not text:
        return None
    match = _SUBTITLE_RE.search(text)
    if not match:
        return None
    word = _SUBTITLE_WORDS.get(match.group("word").lower())
    if word is None:
        return None
    return word, float(match.group("value"))


def _crosscheck(direction: str, threshold: float, subtitle: str) -> bool:
    """Does the human-readable subtitle agree with the structured strike?

    Absent or unreadable subtitle -> True: no cross-check available is not the
    same as a failed one. A subtitle that *disagrees* -> False, because one of
    the two is wrong and there is no way to tell which.
    """
    parsed = _subtitle_terms(subtitle)
    if parsed is None:
        return True
    sub_direction, sub_bound = parsed
    if sub_direction != direction:
        logger.warning(
            "Weather terms: subtitle %r contradicts strike direction %r",
            subtitle, direction,
        )
        return False
    # Strict threshold T implies the inclusive bound is one whole degree inside.
    expected = threshold + 1 if direction == "above" else threshold - 1
    if abs(sub_bound - expected) > 1e-9:
        logger.warning(
            "Weather terms: subtitle bound %s does not match strike %s (%s); "
            "boundary convention is not what we assume",
            sub_bound, threshold, direction,
        )
        return False
    return True


def _from_structured(market) -> tuple:
    """(terms, structured_present).

    The second value matters: "Kalshi published no structured terms" and
    "Kalshi published terms we cannot use" are different situations. Only the
    first may fall back to the title. If the API says `between` and the title
    says ">90°", the title is a lossy summary of a range contract — pricing it
    one-sided would invent a market that does not exist.
    """
    strike_type = (getattr(market, "strike_type", "") or "").strip().lower()
    if not strike_type:
        return None, False

    direction = _STRIKE_TYPES.get(strike_type)
    if direction is None:
        logger.info("Weather terms: unsupported strike_type %r", strike_type)
        return None, True

    # Inclusive variants are normalised by shifting the bound a whole degree,
    # so downstream only ever sees the strict form.
    if direction == "above_inclusive":
        raw = getattr(market, "floor_strike", None)
        terms = None if raw is None else ContractTerms("above", float(raw) - 1, "F", "structured")
    elif direction == "below_inclusive":
        raw = getattr(market, "cap_strike", None)
        terms = None if raw is None else ContractTerms("below", float(raw) + 1, "F", "structured")
    else:
        raw = (
            getattr(market, "floor_strike", None)
            if direction == "above"
            else getattr(market, "cap_strike", None)
        )
        terms = None if raw is None else ContractTerms(direction, float(raw), "F", "structured")

    if terms is None:
        logger.info(
            "Weather terms: strike_type %r with no matching strike value on %s",
            strike_type, getattr(market, "ticker", "?"),
        )
    return terms, True


def _from_title(title: str) -> Optional[ContractTerms]:
    match = _TITLE_RE.search(title or "")
    if not match:
        return None
    direction = _TITLE_OPS.get(match.group("op").lower())
    if direction is None:
        return None
    value = float(match.group("value"))
    if direction == "above_inclusive":
        direction, value = "above", value - 1
    elif direction == "below_inclusive":
        direction, value = "below", value + 1
    return ContractTerms(direction, value, "F", "title")


def is_temperature_market(market) -> bool:
    """Only daily high-temperature contracts are in scope for this parser."""
    title = (getattr(market, "title", "") or "").lower()
    return "temp" in title


def is_unsupported_type(market) -> bool:
    """A contract type we recognise but have not modelled yet.

    Distinct from unreadable: nothing is wrong with the data, we simply do not
    produce interval probabilities yet. Keeping the two apart stops a modelling
    gap from looking like a parser failure.
    """
    strike_type = (getattr(market, "strike_type", "") or "").strip().lower()
    return strike_type in UNSUPPORTED_STRIKE_TYPES


def parse_contract_terms(market) -> Optional[ContractTerms]:
    """Terms for a temperature contract, or None if it cannot be priced.

    None is the honest answer for a contract we cannot read: an unsupported
    strike type, a missing threshold, or a subtitle that contradicts the
    structured fields. The caller records it as unpriceable and the model
    produces no estimate for it.
    """
    if not is_temperature_market(market):
        return None

    terms, structured_present = _from_structured(market)
    if terms is None:
        if structured_present:
            return None  # published terms we cannot use — never guess past them
        terms = _from_title(getattr(market, "title", ""))
    if terms is None:
        return None

    subtitle = (
        getattr(market, "subtitle", "")
        or getattr(market, "yes_sub_title", "")
        or ""
    )
    if not _crosscheck(terms.direction, terms.threshold, subtitle):
        return None
    return terms
