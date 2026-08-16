"""A read-only census of the production database.

Written because the question "why is the DB at 68% of its cap and why does the
funnel say 328,099 open markets" could not be answered at all: the production
DATABASE_URL is an Actions secret, and every local hypothesis was unfalsifiable
from a laptop. Guessing at a storage emergency is how you delete the wrong
table.

Two properties are pinned here. It must count the SAME open-market number the
scoring funnel counts — a re-expression of that query would let the report and
the funnel disagree while both looked right (L26). And it must not write:
a diagnostic that mutates the thing it is measuring is not a diagnostic.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from src.database import ensure_schema, get_engine, get_session
from src.maintenance.db_stats import collect, format_report
from src.maintenance.expire_markets import open_market_count
from src.models.market import Market
from src.models.price import PriceSnapshot


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path/'stats.db'}")
    ensure_schema(engine)
    return engine


NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)


def _market(engine, market_id, status, close_offset_days, created_offset_days=0):
    with get_session(engine) as session:
        session.add(
            Market(
                market_id=market_id,
                title="t",
                category="General",
                close_date=NOW + dt.timedelta(days=close_offset_days),
                status=status,
                created_at=NOW - dt.timedelta(days=created_offset_days),
            )
        )
        session.commit()


def _snapshot(engine, market_id, minutes_ago):
    with get_session(engine) as session:
        session.add(
            PriceSnapshot(
                market_id=market_id,
                yes_bid=40,
                yes_ask=42,
                last_price=41,
                volume=100,
                timestamp=NOW - dt.timedelta(minutes=minutes_ago),
            )
        )
        session.commit()


class TestOpenCount:
    def test_open_count_is_the_funnels_own_number(self, engine):
        """Not a re-expression of the query — literally the same function.

        The funnel's denominator and this report must be incapable of
        disagreeing, or a mismatch becomes a second investigation.
        """
        _market(engine, "A-1", "active", close_offset_days=5)
        _market(engine, "B-1", "active", close_offset_days=-5)   # past close
        _market(engine, "C-1", "closed", close_offset_days=5)

        report = collect(engine, now=NOW)

        assert report.open_markets == open_market_count(engine, now=NOW)
        assert report.open_markets == 1

    def test_separates_open_status_from_genuinely_open(self, engine):
        """The distinction the bloat investigation turns on.

        A row can carry status='active' forever; only close_date says whether
        the market still exists. Reporting one number for both is what let
        ~27,000 corpses read as a live universe.
        """
        _market(engine, "A-1", "active", close_offset_days=5)
        for i in range(4):
            _market(engine, f"DEAD-{i}", "active", close_offset_days=-9)

        report = collect(engine, now=NOW)

        assert report.status_counts["active"] == 5
        assert report.open_markets == 1
        assert report.open_status_past_close == 4


class TestHorizon:
    def test_open_markets_bucketed_by_how_far_out_they_close(self, engine):
        """Distinguishes a real tradeable universe from a far-dated graveyard.

        Nothing beyond the velocity limit can ever be traded, so if the open
        count is dominated by long-dated rows, the number is storage, not
        opportunity.
        """
        _market(engine, "SOON-1", "active", close_offset_days=3)
        _market(engine, "MID-1", "active", close_offset_days=20)
        _market(engine, "FAR-1", "active", close_offset_days=200)
        _market(engine, "FAR-2", "active", close_offset_days=400)

        report = collect(engine, now=NOW)

        assert report.horizon["<=7d"] == 1
        assert report.horizon["8-30d"] == 1
        assert report.horizon[">90d"] == 2
        assert sum(report.horizon.values()) == report.open_markets


class TestGrowth:
    def test_rows_first_seen_per_day(self, engine):
        """Answers "is something accumulating per cycle" with a rate, not a level."""
        _market(engine, "OLD-1", "active", 5, created_offset_days=3)
        _market(engine, "NEW-1", "active", 5, created_offset_days=0)
        _market(engine, "NEW-2", "active", 5, created_offset_days=0)

        report = collect(engine, now=NOW)
        by_day = dict(report.markets_created_per_day)

        assert by_day[NOW.date()] == 2
        assert by_day[(NOW - dt.timedelta(days=3)).date()] == 1


class TestScorerReach:
    def test_counts_markets_the_scorer_can_actually_see(self, engine):
        """The gap between 328,099 "open" and 24 scored is the whole story.

        A market with no fresh snapshot is refused by the stale-data guard, so
        this is the only open-market number that corresponds to work done.
        """
        _market(engine, "FRESH-1", "active", 5)
        _market(engine, "STALE-1", "active", 5)
        _snapshot(engine, "FRESH-1", minutes_ago=5)
        _snapshot(engine, "STALE-1", minutes_ago=600)

        report = collect(engine, now=NOW)

        assert report.markets_with_fresh_snapshot == 1


class TestReadOnly:
    def test_collect_writes_nothing(self, engine):
        """A census that mutates the population is not a census."""
        _market(engine, "A-1", "active", 5)
        _snapshot(engine, "A-1", minutes_ago=5)

        def fingerprint():
            with get_session(engine) as session:
                return {
                    table: session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                    ).scalar_one()
                    for table in ("markets", "price_snapshots")
                } | {
                    "market_rows": session.execute(
                        text("SELECT market_id, status, close_date FROM markets ORDER BY 1")
                    ).all()
                }

        before = fingerprint()
        collect(engine, now=NOW)

        assert fingerprint() == before


class TestReport:
    def test_format_survives_an_empty_database(self, engine):
        """The first production run may legitimately have nothing to say."""
        text_out = format_report(collect(engine, now=NOW))

        assert "open markets" in text_out.lower()

    def test_format_carries_the_numbers_a_decision_needs(self, engine):
        _market(engine, "A-1", "active", 5)
        _market(engine, "DEAD-1", "active", -5)

        text_out = format_report(collect(engine, now=NOW))

        assert "1" in text_out
        assert "past close" in text_out.lower()


class TestRecorderLivenessInTheCensus:
    """The recorder audit rides along with the census.

    "How much of the 244,270 recorded messages was live data?" and "why is the
    DB full?" are one dispatch, not two — and the answer to the first is a
    correction to the day-7 date, so it should not need a second round trip to
    a database only Actions can reach.
    """

    def test_report_carries_the_live_dead_split(self, engine):
        import datetime as dt

        from src.models.orderbook_raw import OrderbookDeltaRaw

        _market(engine, "DEAD-1", "active", close_offset_days=-2)
        with get_session(engine) as session:
            session.add(OrderbookDeltaRaw(
                market_ticker="DEAD-1", msg_type="delta", payload="{}",
                received_at=NOW - dt.timedelta(hours=1),
            ))
            session.commit()

        report = collect(engine, now=NOW)

        assert report.recorder["liveness"]["dead"] == 1
        assert report.recorder["liveness"]["live"] == 0
        assert "dead" in format_report(report).lower()

    def test_an_empty_recorder_table_is_not_an_error(self, engine):
        report = collect(engine, now=NOW)

        assert report.recorder["messages"] == 0
        format_report(report)
