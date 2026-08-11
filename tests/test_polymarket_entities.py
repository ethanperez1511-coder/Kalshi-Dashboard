"""Polymarket match hardening (Phase 1.3, 2026-08-11).

The bug, live in production before this change: `_match_market` compared numeric
*tokens* but not the comparator attached to them.

    if _numbers_in(cand.question) != numbers: continue

So "CPI **above** 3%" and "CPI **below** 3%" have the same tokens, the same
numbers, and ~0.9 similarity — they matched, and the model ingested a
probability that meant the opposite of the market being priced. Negation
("will not happen") is one token out of eight and survived the 0.7 similarity
gate. And bare token sets are symmetric, so "Yankees beat Red Sox" scored
identically against "Red Sox beat Yankees".

A wrong match is fabricated data pointed straight at the sizing engine, so the
policy is fail-closed: anything short of a clean entity match produces **no
estimate** and lands in a review queue for a human to approve once.
"""
from __future__ import annotations

import pytest

from src.database import Base, get_session
from src.modeling.entities import compare, extract
from src.modeling.models.polymarket import PolymarketModel
from src.modeling.polymarket_api import PolyMarket
from src.models.match_map import MarketMatchMap


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

class TestExtraction:
    def test_direction_is_captured_not_just_the_number(self):
        above = extract("Will CPI be above 3% in 2026?")
        below = extract("Will CPI be below 3% in 2026?")
        assert [t.direction for t in above.thresholds] == ["above"]
        assert [t.direction for t in below.thresholds] == ["below"]
        assert above.thresholds[0].value == below.thresholds[0].value == 3.0

    @pytest.mark.parametrize("title,direction", [
        ("Will BTC exceed $100000 by 2026?", "above"),
        ("Will BTC reach $100000 by 2026?", "above"),
        ("Will inflation be at least 3%?", "above"),
        ("Will unemployment fall under 4%?", "below"),
        ("Will the deficit be less than 5%?", "below"),
        ("Will GDP be 3% or higher?", "above"),
        ("Will GDP be 3% or lower?", "below"),
        ("Will the rate be > 4.5%?", "above"),
        ("Will the rate be <= 4.5%?", "below"),
    ])
    def test_comparator_vocabulary(self, title, direction):
        assert extract(title).thresholds[0].direction == direction

    def test_negation_is_detected(self):
        assert extract("Will Smith not win the election in 2026?").negated is True
        assert extract("Will Smith win the election in 2026?").negated is False
        assert extract("Will the bill fail to pass by 2026?").negated is True

    def test_named_entity_order_is_preserved(self):
        a = extract("Will Yankees beat Red Sox on August 11?")
        assert a.named.index("Yankees") < a.named.index("Red Sox")

    def test_dates_are_normalised(self):
        assert "2026" in extract("Will X happen in 2026?").dates
        assert "2026-07" in extract("Will X happen before July 2026?").dates


# --------------------------------------------------------------------------
# comparison — the conflicts that were silently matching
# --------------------------------------------------------------------------

class TestConflicts:
    def test_above_vs_below_is_a_conflict(self):
        """THE production bug. Same words, same numbers, opposite meaning."""
        v = compare(
            extract("Will CPI be above 3% in 2026?"),
            extract("Will CPI be below 3% in 2026?"),
        )
        assert v.verdict == "conflict"
        assert any("direction" in r for r in v.reasons)

    def test_negation_flip_is_a_conflict(self):
        v = compare(
            extract("Will Israel have a new PM in 2026?"),
            extract("Will Israel not have a new PM in 2026?"),
        )
        assert v.verdict == "conflict"

    def test_subject_object_swap_is_a_conflict(self):
        v = compare(
            extract("Will Yankees beat Red Sox on August 11?"),
            extract("Will Red Sox beat Yankees on August 11?"),
        )
        assert v.verdict == "conflict"

    def test_different_threshold_value_is_a_conflict(self):
        v = compare(
            extract("Will CPI be above 3% in 2026?"),
            extract("Will CPI be above 4% in 2026?"),
        )
        assert v.verdict == "conflict"

    def test_different_date_is_a_conflict(self):
        v = compare(
            extract("Will X happen in 2026?"),
            extract("Will X happen in 2027?"),
        )
        assert v.verdict == "conflict"

    def test_shared_first_name_is_not_a_match(self):
        """Found against live Polymarket data on 2026-08-11.

        Both the fuzzy matcher AND the first cut of this entity check accepted
        these: identical sentence, one surname different, two different
        candidates for the same office. Whichever price we took was the wrong
        person's. Non-ASCII names must survive extraction intact too — the
        original regex mangled "Cătălin" and made the problem worse.
        """
        v = compare(
            extract("Will Alexandru Rafila be the next Prime Minister of Romania?"),
            extract("Will Alexandru Nazare be the next Prime Minister of Romania?"),
        )
        assert v.verdict == "conflict"
        assert "Rafila" in v.reasons[0] or "Nazare" in v.reasons[0]

        v2 = compare(
            extract("Will Cătălin Drulă be the next Prime Minister of Romania?"),
            extract("Will Cătălin Predoiu be the next Prime Minister of Romania?"),
        )
        assert v2.verdict == "conflict"

    def test_accented_names_are_extracted_whole(self):
        assert "Cătălin Predoiu" in extract(
            "Will Cătălin Predoiu be the next Prime Minister of Romania?"
        ).named

    def test_clean_restatement_matches(self):
        v = compare(
            extract("Will CPI be above 3% in 2026?"),
            extract("Will CPI be above 3% in 2026?"),
        )
        assert v.verdict == "match"

    def test_one_sided_threshold_is_insufficient_not_a_match(self):
        v = compare(
            extract("Will CPI be above 3% in 2026?"),
            extract("Will CPI rise in 2026?"),
        )
        assert v.verdict in ("insufficient", "conflict")
        assert v.verdict != "match"


# --------------------------------------------------------------------------
# model behaviour: fail closed, then remember the decision
# --------------------------------------------------------------------------

def _poly(question, price=0.62, volume=500_000.0, cid="0xabc"):
    return PolyMarket(
        condition_id=cid, question=question, yes_price=price, volume_usd=volume,
    )


class _FakeClient:
    def __init__(self, markets):
        self._markets = markets

    def get_markets(self):
        return self._markets


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


# Realistic phrasing: long enough that a one-word direction flip still clears
# the 0.7 similarity gate. Measured 0.818 similarity with identical numeric
# tokens, i.e. exactly the shape that was matching in production.
KALSHI_ABOVE = "Will the US Consumer Price Index inflation rate be above 3% in December 2026?"
POLY_BELOW = "Will the US Consumer Price Index inflation rate be below 3% in December 2026?"


class TestModelFailsClosed:
    def test_the_fuzzy_matcher_alone_would_have_accepted_it(self):
        """Documents why the entity layer exists: similarity says yes."""
        from src.modeling.models.polymarket import _match_market, _normalize_tokens, _similarity

        assert _similarity(
            _normalize_tokens(KALSHI_ABOVE), _normalize_tokens(POLY_BELOW)
        ) > 0.7
        assert _match_market(KALSHI_ABOVE, [_poly(POLY_BELOW)]) is not None

    def test_direction_conflict_produces_no_estimate(self, engine):
        model = PolymarketModel(_FakeClient([_poly(POLY_BELOW, price=0.30)]))
        assert model.estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine) is None, (
            "took a price that meant the opposite"
        )

    def test_conflict_is_queued_for_review(self, engine):
        model = PolymarketModel(_FakeClient([_poly(POLY_BELOW)]))
        model.estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine)
        with get_session(engine) as s:
            row = s.query(MarketMatchMap).filter_by(kalshi_market_id="KX-CPI").one()
            assert row.status == "pending"
            assert "direction" in (row.reason or "")
            assert row.kalshi_title == KALSHI_ABOVE
            assert row.poly_question == POLY_BELOW

    def test_pending_stays_silent_until_approved(self, engine):
        client = _FakeClient([_poly(POLY_BELOW)])
        PolymarketModel(client).estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine)
        # Second pass, same uncertain pair: still no estimate, still one row.
        assert PolymarketModel(client).estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine) is None
        with get_session(engine) as s:
            assert s.query(MarketMatchMap).filter_by(kalshi_market_id="KX-CPI").count() == 1

    def test_clean_match_still_produces_an_estimate(self, engine):
        """The tightening must not blind the model to genuine matches."""
        model = PolymarketModel(_FakeClient([_poly(KALSHI_ABOVE, price=0.62)]))
        result = model.estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine)
        assert result is not None
        assert result.p_model == 0.62

    def test_clean_match_is_remembered(self, engine):
        model = PolymarketModel(_FakeClient([_poly(KALSHI_ABOVE)]))
        model.estimate("KX-CPI", KALSHI_ABOVE, 0.5, engine)
        with get_session(engine) as s:
            row = s.query(MarketMatchMap).filter_by(kalshi_market_id="KX-CPI").one()
            assert row.status == "approved"
            assert row.decided_by == "entity_match"


class TestRememberedDecisions:
    def test_blocked_pair_is_never_matched_again(self, engine):
        with get_session(engine) as s:
            s.add(MarketMatchMap(
                kalshi_market_id="KX-1", poly_condition_id="0xabc",
                status="blocked", similarity=1.0, decided_by="human",
            ))
            s.commit()
        model = PolymarketModel(_FakeClient([_poly("Will X happen in 2026?")]))
        assert model.estimate("KX-1", "Will X happen in 2026?", 0.5, engine) is None

    def test_approved_mapping_is_reused_without_fuzzy_matching(self, engine):
        """A human approval must survive a title the fuzzy matcher would reject —
        that is the entire point of approving it once."""
        with get_session(engine) as s:
            s.add(MarketMatchMap(
                kalshi_market_id="KX-2", poly_condition_id="0xdef",
                status="approved", similarity=0.4, decided_by="human",
            ))
            s.commit()
        model = PolymarketModel(_FakeClient([
            _poly("Totally different wording", price=0.71, cid="0xdef")
        ]))
        result = model.estimate("KX-2", "Kalshi phrasing nothing alike", 0.5, engine)
        assert result is not None
        assert result.p_model == 0.71
        assert "approved" in result.reasoning.lower()

    def test_approved_mapping_whose_market_vanished_stays_silent(self, engine):
        with get_session(engine) as s:
            s.add(MarketMatchMap(
                kalshi_market_id="KX-3", poly_condition_id="0xgone",
                status="approved", similarity=1.0, decided_by="human",
            ))
            s.commit()
        model = PolymarketModel(_FakeClient([_poly("Something else", cid="0xother")]))
        assert model.estimate("KX-3", "Anything", 0.5, engine) is None


class TestResolutionHorizon:
    """Identical titles, different resolution windows (Phase 1.5).

    Measured against live data on 2026-08-11: ALL 13 pairs that the entity
    check was passing had a ~6,577-day horizon gap. Not a metadata artefact —
    the Polymarket contracts state "resolve to the next individual ... sworn in
    following the 2026 parliamentary election ... If no such Prime Minister is
    sworn in by December 31, 2027 ... resolve to 'Other'", while the Kalshi
    equivalents have no election scoping and close in 2045.

    P(short-dated) is bounded above by P(long-dated) and the gap only ever
    points one way, so every one of those pairs was feeding a downward-biased
    p_model that reads as a durable "NO is cheap" edge.
    """

    def _market(self, engine, close_date):
        from src.models.market import Market
        with get_session(engine) as s:
            s.add(Market(
                market_id="KX-PM", title="Will X be the next Prime Minister?",
                category="Politics", close_date=close_date, status="open",
            ))
            s.commit()

    def _poly(self, end_date):
        return PolyMarket(
            condition_id="0xpm", question="Will X be the next Prime Minister?",
            yes_price=0.68, volume_usd=1_500_000.0, end_date=end_date,
        )

    def test_far_apart_horizons_produce_no_estimate(self, engine):
        from datetime import datetime, timezone
        self._market(engine, datetime(2045, 1, 1, tzinfo=timezone.utc))
        model = PolymarketModel(_FakeClient([
            self._poly(datetime(2026, 12, 31, tzinfo=timezone.utc))
        ]))
        assert model.estimate(
            "KX-PM", "Will X be the next Prime Minister?", 0.5, engine,
        ) is None

    def test_horizon_mismatch_is_queued_with_the_numbers(self, engine):
        from datetime import datetime, timezone
        self._market(engine, datetime(2045, 1, 1, tzinfo=timezone.utc))
        PolymarketModel(_FakeClient([
            self._poly(datetime(2026, 12, 31, tzinfo=timezone.utc))
        ])).estimate("KX-PM", "Will X be the next Prime Minister?", 0.5, engine)
        with get_session(engine) as s:
            row = s.query(MarketMatchMap).filter_by(kalshi_market_id="KX-PM").one()
            assert row.status == "pending"
            assert "horizon" in row.reason
            assert "2045-01-01" in row.reason

    def test_aligned_horizons_still_price(self, engine):
        """The gate must not swallow genuinely aligned pairs."""
        from datetime import datetime, timezone
        self._market(engine, datetime(2026, 12, 20, tzinfo=timezone.utc))
        result = PolymarketModel(_FakeClient([
            self._poly(datetime(2026, 12, 31, tzinfo=timezone.utc))
        ])).estimate("KX-PM", "Will X be the next Prime Minister?", 0.5, engine)
        assert result is not None
        assert result.p_model == 0.68

    def test_unknown_polymarket_horizon_does_not_invent_a_conflict(self, engine):
        from datetime import datetime, timezone
        self._market(engine, datetime(2045, 1, 1, tzinfo=timezone.utc))
        result = PolymarketModel(_FakeClient([self._poly(None)])).estimate(
            "KX-PM", "Will X be the next Prime Minister?", 0.5, engine,
        )
        assert result is not None  # absent data is not evidence of a mismatch


class TestFeedFailureStillFailsSafe:
    def test_feed_down_returns_none(self, engine):
        class Dead:
            def get_markets(self):
                raise RuntimeError("feed down")

        assert PolymarketModel(Dead()).estimate("KX-9", "Will X happen?", 0.5, engine) is None
