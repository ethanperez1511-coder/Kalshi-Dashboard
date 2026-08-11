"""Structured comparison of two prediction-market questions.

Token-similarity matching cannot tell "CPI above 3%" from "CPI below 3%": same
words, same numbers, opposite meaning. It also cannot tell "X will happen" from
"X will not happen", or "A beats B" from "B beats A" — set intersection is
symmetric. Each of those was an accepted match in production, and an accepted
match feeds a probability straight into position sizing.

So titles are reduced to the fields that carry the meaning — direction,
magnitude, date, negation, and the order of the named parties — and compared
field by field. Any field that disagrees is a hard reject no matter how high
the word overlap is.

The extractor is deliberately blunt. It errs toward "conflict" or
"insufficient", both of which mean *no estimate* and a queued review, so a
false alarm costs coverage while a false match costs money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Words that push a threshold up or down. Longest-first so "at least" wins
# over "least" and "or higher" is not shadowed by "higher".
_ABOVE = [
    "at least", "no less than", "greater than", "more than", "or higher",
    "or above", "or more", "or better", "above", "over", "exceeds", "exceed",
    "reaches", "reach", "hits", "hit", "surpasses", "surpass", ">=", ">", "≥",
]
_BELOW = [
    "at most", "no more than", "less than", "fewer than", "or lower",
    "or below", "or less", "below", "under", "beneath", "falls under",
    "<=", "<", "≤",
]

_NEGATIONS = [
    r"\bnot\b", r"\bwon'?t\b", r"\bdoes\s+not\b", r"\bdo\s+not\b",
    r"\bfails?\s+to\b", r"\bfailed\s+to\b", r"\bnever\b", r"\bwithout\b",
    r"\bno\s+longer\b",
]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Sentence scaffolding that starts a question but is never a named party.
_LEADING_STOPWORDS = {
    "will", "who", "what", "when", "which", "how", "is", "are", "does", "do",
    "the", "a", "an", "by", "in", "on", "at", "before", "after", "and", "or",
    "to", "of", "for", "be", "have", "has", "if", "no", "not", "than", "then",
    "there", "this", "that", "it", "its",
}


@dataclass(frozen=True)
class Threshold:
    """A numeric bound plus the direction that gives it meaning."""
    direction: str  # "above" | "below" | "exact"
    value: float

    def key(self) -> Tuple[str, float]:
        return (self.direction, round(self.value, 6))


@dataclass
class MarketEntities:
    named: Tuple[str, ...] = ()          # ordered: order distinguishes A-beats-B
    thresholds: Tuple[Threshold, ...] = ()
    dates: Tuple[str, ...] = ()          # normalised: "2026", "2026-07", "2026-07-04"
    negated: bool = False
    raw: str = ""
    # Every capitalised word, individually. Comparing whole phrases is not
    # enough: "Alexandru Rafila" and "Alexandru Nazare" share a first name, and
    # a shared-token test called that a match against real Polymarket data —
    # two different candidates for the same office. Each token must appear on
    # both sides for the pair to be the same event.
    name_tokens: Tuple[str, ...] = ()
    word_set: frozenset = frozenset()    # all lowercased words, for containment


@dataclass
class EntityVerdict:
    verdict: str  # "match" | "conflict" | "insufficient"
    reasons: List[str] = field(default_factory=list)

    @property
    def is_match(self) -> bool:
        return self.verdict == "match"


def _direction_for(window: str) -> Optional[str]:
    """Classify the comparator nearest a number, or None if there isn't one."""
    low = window.lower()
    hits = []
    for phrase in _ABOVE:
        idx = low.rfind(phrase)
        if idx >= 0:
            hits.append((idx, "above", len(phrase)))
    for phrase in _BELOW:
        idx = low.rfind(phrase)
        if idx >= 0:
            hits.append((idx, "below", len(phrase)))
    if not hits:
        return None
    # Nearest comparator to the number wins; longer phrase breaks a tie so
    # "at least" is not read as the bare ">"-ish fragment inside it.
    hits.sort(key=lambda h: (-h[0], -h[2]))
    return hits[0][1]


def _extract_thresholds(title: str) -> Tuple[Threshold, ...]:
    """Numbers that carry a comparator, paired with its direction.

    A number with no comparator anywhere near it (a year, a jersey number) is
    not a threshold and is ignored here — dates are handled separately.
    """
    out: List[Threshold] = []
    for m in re.finditer(r"\$?\s*(\d+(?:\.\d+)?)\s*(%|percent|bps)?", title):
        value = float(m.group(1))
        unit = (m.group(2) or "").lower()

        # A bare 4-digit year is a date, not a magnitude.
        if not unit and 1900 <= value <= 2100 and value == int(value):
            continue

        before = title[max(0, m.start() - 40): m.start()]
        after = title[m.end(): m.end() + 20]
        direction = _direction_for(before)
        if direction is None:
            # Trailing forms: "3% or higher", "4 or less".
            direction = _direction_for(after)
        if direction is None:
            continue
        out.append(Threshold(direction=direction, value=value))
    return tuple(out)


def _extract_dates(title: str) -> Tuple[str, ...]:
    found: List[str] = []

    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", title):
        found.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")

    for m in re.finditer(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})?,?\s*(\d{4})\b", title
    ):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            continue
        found.append(f"{m.group(3)}-{month:02d}")

    for m in re.finditer(r"\b(19|20)\d{2}\b", title):
        year = m.group(0)
        if not any(f.startswith(year) for f in found):
            found.append(year)

    return tuple(dict.fromkeys(found))


# Unicode-aware: "Cătălin" must be one word, not a mangled ASCII fragment.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _words(title: str) -> List[str]:
    return _WORD_RE.findall(title)


def _extract_named(title: str) -> Tuple[str, ...]:
    """Ordered proper-noun phrases.

    Order matters: "Yankees beat Red Sox" and "Red Sox beat Yankees" have
    identical entity *sets* and opposite meanings.
    """
    named: List[str] = []
    current: List[str] = []

    for word in _words(title):
        is_name = word[:1].isupper() and word.lower() not in _LEADING_STOPWORDS
        if is_name:
            current.append(word)
            continue
        if current:
            phrase = " ".join(current)
            if phrase not in named:
                named.append(phrase)
            current = []
    if current:
        phrase = " ".join(current)
        if phrase not in named:
            named.append(phrase)

    return tuple(named)


def _extract_name_tokens(title: str) -> Tuple[str, ...]:
    """Capitalised words individually — surnames must be checked one by one."""
    return tuple(
        w for w in _words(title)
        if w[:1].isupper() and w.lower() not in _LEADING_STOPWORDS
    )


def extract(title: str) -> MarketEntities:
    """Reduce a market question to the fields that carry its meaning."""
    title = (title or "").strip()
    negated = any(re.search(p, title, re.IGNORECASE) for p in _NEGATIONS)
    return MarketEntities(
        named=_extract_named(title),
        thresholds=_extract_thresholds(title),
        dates=_extract_dates(title),
        negated=negated,
        raw=title,
        name_tokens=_extract_name_tokens(title),
        word_set=frozenset(w.lower() for w in _words(title)),
    )


def _dates_conflict(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    """True when both sides name a date and neither refines the other.

    "2026" vs "2026-07" is a refinement, not a disagreement; "2026" vs "2027"
    is a different market.
    """
    if not a or not b:
        return False
    for da in a:
        for db in b:
            if da == db or da.startswith(db) or db.startswith(da):
                return False
    return True


def compare(a: MarketEntities, b: MarketEntities) -> EntityVerdict:
    """Field-by-field verdict on whether two questions describe one event.

    Returns "conflict" when a field actively disagrees, "insufficient" when one
    side carries a qualifier the other lacks, and "match" only when everything
    that could distinguish the two agrees. Only "match" may produce a price.
    """
    reasons: List[str] = []

    if a.negated != b.negated:
        reasons.append(
            f"negation differs: {'negated' if a.negated else 'plain'} vs "
            f"{'negated' if b.negated else 'plain'}"
        )
        return EntityVerdict("conflict", reasons)

    ka = sorted(t.key() for t in a.thresholds)
    kb = sorted(t.key() for t in b.thresholds)
    if ka and kb and ka != kb:
        dirs_a = {d for d, _ in ka}
        dirs_b = {d for d, _ in kb}
        if dirs_a != dirs_b:
            reasons.append(f"threshold direction differs: {sorted(dirs_a)} vs {sorted(dirs_b)}")
        else:
            reasons.append(f"threshold value differs: {ka} vs {kb}")
        return EntityVerdict("conflict", reasons)

    if _dates_conflict(a.dates, b.dates):
        reasons.append(f"date differs: {list(a.dates)} vs {list(b.dates)}")
        return EntityVerdict("conflict", reasons)

    # Every named word must appear on both sides. A shared *phrase* test is not
    # enough — measured against live Polymarket data, "Alexandru Rafila" and
    # "Alexandru Nazare" (two different candidates for the same office) passed
    # it on the strength of a shared first name.
    if a.name_tokens and b.name_tokens:
        only_a = [t for t in a.name_tokens if t.lower() not in b.word_set]
        only_b = [t for t in b.name_tokens if t.lower() not in a.word_set]
        if only_a or only_b:
            reasons.append(
                f"named entity present on one side only: "
                f"{only_a or '—'} vs {only_b or '—'}"
            )
            return EntityVerdict("conflict", reasons)

        set_a, set_b = set(a.named), set(b.named)
        shared = set_a & set_b
        if len(shared) >= 2:
            # Same parties, different order — "A beats B" vs "B beats A".
            order_a = [n for n in a.named if n in shared]
            order_b = [n for n in b.named if n in shared]
            if order_a != order_b:
                reasons.append(f"named entity order differs: {order_a} vs {order_b}")
                return EntityVerdict("conflict", reasons)

    # Nothing disagrees. Is there enough to affirm a match?
    if bool(ka) != bool(kb):
        reasons.append("one side states a numeric threshold and the other does not")
        return EntityVerdict("insufficient", reasons)
    if not a.name_tokens or not b.name_tokens:
        reasons.append("no named entity extracted on one side")
        return EntityVerdict("insufficient", reasons)
    if bool(a.dates) != bool(b.dates):
        reasons.append("one side states a date and the other does not")
        return EntityVerdict("insufficient", reasons)

    return EntityVerdict("match", ["all extracted fields agree"])
