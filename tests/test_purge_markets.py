"""Reclaiming the parlay graveyard without losing anything that was history.

The standing rule is archive, never delete. The amendment that makes this
action possible is narrow and stated by the operator: a market row with ZERO
dependent data — no price snapshots, no trades, no positions, no opportunities,
no recorded book deltas — is noise, not history. Nothing the backtester or the
day-7 measurement could ever read is attached to it, so hard-deleting it loses
exactly nothing.

Everything with a dependent row keeps its row. It is status-corrected out of
the open universe instead, which costs nothing and preserves the join.

Two absolute exemptions, and they are checked before anything else. A market
with an open position, or with an opportunity inside the recorder's recency
window, is not touched at all — not deleted, not archived. `markets_to_record`
joins `markets` and requires an open status, so archiving one of those rows
would blind the book recorder on precisely the markets it most needs to tape,
and that data cannot be backfilled.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.maintenance.purge_markets import (
    CONFIRM_TOKEN,
    execute_purge,
    format_plan,
    plan_purge,
)
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.trade import Trade

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(engine, market_id, close_offset_days=-1, status="active"):
    with get_session(engine) as s:
        s.add(Market(
            market_id=market_id, title="t", category="General",
            close_date=NOW + dt.timedelta(days=close_offset_days), status=status,
        ))
        s.commit()


def _ids(engine):
    with get_session(engine) as s:
        return sorted(r[0] for r in s.execute(
            __import__("sqlalchemy").select(Market.market_id)
        ).all())


def _status(engine, market_id):
    with get_session(engine) as s:
        return s.execute(
            __import__("sqlalchemy").select(Market.status)
            .where(Market.market_id == market_id)
        ).scalar_one()


class TestOrphanIdentification:
    def test_an_excluded_series_orphan_is_deletable(self, engine):
        _market(engine, "KXMVECROSSCATEGORY-26AUG17-A", close_offset_days=5)

        plan = plan_purge(engine, now=NOW)

        assert plan.deletable == 1
        assert plan.archivable == 0

    def test_a_past_close_orphan_is_deletable(self, engine):
        _market(engine, "KXDEAD-26AUG10-A", close_offset_days=-5)

        assert plan_purge(engine, now=NOW).deletable == 1

    def test_a_live_market_outside_an_excluded_series_is_left_alone(self, engine):
        """Still tradeable. Not a candidate at all, orphan or not."""
        _market(engine, "KXHIGHNY-26AUG18-T91", close_offset_days=1)

        plan = plan_purge(engine, now=NOW)

        assert plan.deletable == 0
        assert plan.archivable == 0


class TestDependentsAreNeverDeleted:
    @pytest.mark.parametrize("kind", ["snapshot", "trade", "position", "opportunity", "delta"])
    def test_any_dependent_row_downgrades_delete_to_archive(self, engine, kind):
        _market(engine, "KXMVECROSSCATEGORY-26AUG10-A", close_offset_days=-5)
        with get_session(engine) as s:
            if kind == "snapshot":
                s.add(PriceSnapshot(
                    market_id="KXMVECROSSCATEGORY-26AUG10-A", yes_bid=1, yes_ask=2,
                    last_price=1, volume=1,
                ))
            elif kind == "trade":
                s.add(Trade(
                    market_id="KXMVECROSSCATEGORY-26AUG10-A", side="yes", action="buy",
                    price=50, quantity=1, p_model=0.5, implied_prob=0.5, edge=0.0,
                    net_ev=0.0, position_size_dollars=0.5, confidence=0.5,
                    reasoning="t", is_paper=True, status="filled", model_name="X",
                ))
            elif kind == "position":
                s.add(Position(
                    market_id="KXMVECROSSCATEGORY-26AUG10-A", side="yes",
                    entry_price=50, quantity=1, current_price=50, status="closed",
                ))
            elif kind == "opportunity":
                s.add(Opportunity(
                    market_id="KXMVECROSSCATEGORY-26AUG10-A", p_model=0.5,
                    implied_prob=0.5, edge=0.0, net_ev=0.0, recommended_side="yes",
                    confidence=0.5, status="rejected", model_name="X",
                    scored_at=NOW - dt.timedelta(days=30),
                ))
            else:
                s.add(OrderbookDeltaRaw(
                    market_ticker="KXMVECROSSCATEGORY-26AUG10-A",
                    msg_type="delta", payload="{}",
                ))
            s.commit()

        plan = plan_purge(engine, now=NOW)

        assert plan.deletable == 0
        assert plan.archivable == 1

    def test_execution_keeps_the_row_and_corrects_its_status(self, engine):
        _market(engine, "KXMVECROSSCATEGORY-26AUG10-A", close_offset_days=-5)
        with get_session(engine) as s:
            s.add(PriceSnapshot(
                market_id="KXMVECROSSCATEGORY-26AUG10-A", yes_bid=1, yes_ask=2,
                last_price=1, volume=1,
            ))
            s.commit()

        execute_purge(engine, plan_purge(engine, now=NOW), now=NOW)

        assert _ids(engine) == ["KXMVECROSSCATEGORY-26AUG10-A"]
        assert _status(engine, "KXMVECROSSCATEGORY-26AUG10-A") == "archived"


class TestExemptions:
    """The constraint that protects the recorder. Checked before everything."""

    def test_a_market_with_an_open_position_is_untouched(self, engine):
        _market(engine, "KXMVECROSSCATEGORY-26AUG10-A", close_offset_days=-5)
        with get_session(engine) as s:
            s.add(Position(
                market_id="KXMVECROSSCATEGORY-26AUG10-A", side="yes",
                entry_price=50, quantity=1, current_price=50, status="open",
            ))
            s.commit()

        plan = plan_purge(engine, now=NOW)
        execute_purge(engine, plan, now=NOW)

        assert plan.deletable == 0
        assert plan.archivable == 0
        assert plan.exempt == 1
        assert _status(engine, "KXMVECROSSCATEGORY-26AUG10-A") == "active"

    def test_a_market_with_a_recent_opportunity_is_untouched(self, engine):
        """`markets_to_record` joins markets and needs an open status; archiving
        this row would blind the recorder on a market it is taping right now."""
        _market(engine, "KXMVECROSSCATEGORY-26AUG18-A", close_offset_days=2)
        with get_session(engine) as s:
            s.add(Opportunity(
                market_id="KXMVECROSSCATEGORY-26AUG18-A", p_model=0.5,
                implied_prob=0.5, edge=0.0, net_ev=0.0, recommended_side="yes",
                confidence=0.5, status="qualifying", model_name="X",
                scored_at=NOW - dt.timedelta(hours=1),
            ))
            s.commit()

        plan = plan_purge(engine, now=NOW)

        assert plan.exempt == 1
        assert plan.deletable == 0
        assert plan.archivable == 0

    def test_a_stale_opportunity_does_not_exempt(self, engine):
        """Outside the recency window the recorder would not subscribe to it
        anyway, so the row is archivable — but still not deletable."""
        _market(engine, "KXMVECROSSCATEGORY-26AUG10-A", close_offset_days=-5)
        with get_session(engine) as s:
            s.add(Opportunity(
                market_id="KXMVECROSSCATEGORY-26AUG10-A", p_model=0.5,
                implied_prob=0.5, edge=0.0, net_ev=0.0, recommended_side="yes",
                confidence=0.5, status="rejected", model_name="X",
                scored_at=NOW - dt.timedelta(days=10),
            ))
            s.commit()

        plan = plan_purge(engine, now=NOW)

        assert plan.exempt == 0
        assert plan.archivable == 1


class TestDryRunIsADryRun:
    def test_planning_changes_nothing(self, engine):
        _market(engine, "KXMVECROSSCATEGORY-26AUG17-A", close_offset_days=5)

        plan_purge(engine, now=NOW)

        assert _ids(engine) == ["KXMVECROSSCATEGORY-26AUG17-A"]

    def test_execution_removes_the_orphans(self, engine):
        for i in range(5):
            _market(engine, f"KXMVECROSSCATEGORY-26AUG17-{i}", close_offset_days=5)
        _market(engine, "KXHIGHNY-26AUG18-T91", close_offset_days=1)

        result = execute_purge(engine, plan_purge(engine, now=NOW), now=NOW)

        assert result["deleted"] == 5
        assert _ids(engine) == ["KXHIGHNY-26AUG18-T91"]


class TestReport:
    def test_the_plan_names_the_series_and_the_bytes(self, engine):
        for i in range(3):
            _market(engine, f"KXMVECROSSCATEGORY-26AUG17-{i}", close_offset_days=5)

        text = format_plan(plan_purge(engine, now=NOW))

        assert "KXMVECROSSCATEGORY" in text
        assert "3" in text
        assert CONFIRM_TOKEN in text

    def test_an_empty_database_produces_a_plan_not_an_error(self, engine):
        plan = plan_purge(engine, now=NOW)

        assert plan.deletable == 0
        format_plan(plan)
