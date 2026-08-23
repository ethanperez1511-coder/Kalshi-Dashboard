"""Rewriting an unchanged market every five minutes is pure transfer cost.

`sync_markets` split its batch into inserts and updates by "does the row
exist", so every market Kalshi returned was UPDATEd every cycle — title, rules
and all — whether or not anything about it had changed. Titles run to 1,381
characters on parlays and `rules` holds the full contract text, so that is the
second-largest consumer of the transfer quota that stopped production.

Detection is by content hash rather than by comparing the fields themselves.
Fetching title+rules to decide whether to write title+rules costs the same
transfer as writing them; a 32-character digest costs ~4% of that.

The hash must cover EVERY field written. Kalshi reworded the settlement clause
of all seven temperature series on 2026-08-14 without touching anything else —
a comparison that skipped `rules` would have persisted the old text forever and
left the settlement guard verifying a string that was no longer on the market.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from src.database import Base, get_session
from src.ingestion.market_sync import sync_markets
from src.kalshi.schemas import KalshiMarket
from src.models.market import Market

CLOSE = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(ticker="KXHIGHNY-26AUG24-T90", title="Will it be >90?", rules="CLINYC",
            status="active", category="General"):
    return KalshiMarket(
        ticker=ticker, title=title, category=category, close_time=CLOSE,
        status=status, rules_primary=rules, yes_bid=44, yes_ask=46,
        last_price=45, volume=100,
    )


def _updated_rows(engine, markets):
    """sync_markets returns inserts; this reports what it chose to UPDATE."""
    from src.ingestion import market_sync

    seen = {}
    real = market_sync._write_rows

    def _spy(session, inserts, updates):
        seen["inserts"] = len(inserts)
        seen["updates"] = len(updates)
        return real(session, inserts, updates)

    market_sync._write_rows = _spy
    try:
        sync_markets(engine, markets)
    finally:
        market_sync._write_rows = real
    return seen


class TestUnchangedMarketsAreNotRewritten:
    def test_a_second_identical_sync_writes_nothing(self, engine):
        sync_markets(engine, [_market()])

        seen = _updated_rows(engine, [_market()])

        assert seen["inserts"] == 0
        assert seen["updates"] == 0

    def test_the_first_sync_still_inserts(self, engine):
        seen = _updated_rows(engine, [_market()])

        assert seen["inserts"] == 1

    def test_only_the_changed_market_is_written(self, engine):
        batch = [_market(f"KXHIGHNY-26AUG24-T{n}") for n in (90, 91, 92)]
        sync_markets(engine, batch)

        batch[1] = _market("KXHIGHNY-26AUG24-T91", status="closed")
        seen = _updated_rows(engine, batch)

        assert seen["updates"] == 1


class TestEveryWrittenFieldIsCovered:
    @pytest.mark.parametrize("field,value", [
        ("title", "Will it be >95?"),
        ("rules", "... according to The Weather Company ..."),
        ("status", "closed"),
        ("category", "Weather"),
    ])
    def test_a_change_to_any_field_triggers_a_write(self, engine, field, value):
        sync_markets(engine, [_market()])

        seen = _updated_rows(engine, [_market(**{field: value})])

        assert seen["updates"] == 1, f"a change to {field} was not detected"

    def test_a_rules_rewording_is_persisted(self, engine):
        """The 2026-08-14 settlement change, which touched only this field."""
        sync_markets(engine, [_market(rules="NWS Climatological Report")])
        sync_markets(engine, [_market(rules="CLINYC per The Weather Company")])

        with get_session(engine) as session:
            stored = session.execute(select(Market.rules)).scalar_one()

        assert "Weather Company" in stored

    def test_a_close_date_change_triggers_a_write(self, engine):
        sync_markets(engine, [_market()])
        moved = _market()
        moved.close_time = CLOSE + dt.timedelta(days=1)

        seen = _updated_rows(engine, [moved])

        assert seen["updates"] == 1


class TestCorrectnessIsUnchanged:
    def test_the_row_still_lands_with_every_field(self, engine):
        sync_markets(engine, [_market(title="T", rules="R", category="Weather")])

        with get_session(engine) as session:
            row = session.execute(
                select(Market.title, Market.rules, Market.category)
            ).one()

        assert tuple(row) == ("T", "R", "Weather")

    def test_rows_with_no_hash_yet_are_written_once(self, engine):
        """Existing production rows predate the column; they migrate on first
        sight and then go quiet."""
        sync_markets(engine, [_market()])
        with get_session(engine) as session:
            session.execute(
                Market.__table__.update().values(content_hash=None)
            )
            session.commit()

        assert _updated_rows(engine, [_market()])["updates"] == 1
        assert _updated_rows(engine, [_market()])["updates"] == 0
