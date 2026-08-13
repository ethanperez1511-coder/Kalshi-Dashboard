"""Why a qualifying opportunity did not trade, and whether its alert landed.

Six opportunities qualified and one traded. The reason for the other five went
to `logger.info` and nowhere else, so the question could not be answered after
the fact — the same hole the scoring funnel was built to close, one layer down.

Alert delivery is here too. `_send` swallowed every exception into a warning
and returned None, so "the trade alerted" and "the trade silently failed to
alert" were the same observable state.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.execution.funnel import ExecutionFunnel
from src.models.market import Market


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


# --------------------------------------------------------------------------
# 1. The partition
# --------------------------------------------------------------------------

class TestExecutionFunnel:
    def test_every_qualifying_opportunity_is_attributed(self):
        f = ExecutionFunnel(qualifying=6)
        f.record_rejection(["Position size too small"])
        f.record_rejection(["Cluster exposure $12.40 exceeds max $10.00 for cluster 26AUG13"])
        f.record_rejection(["Cluster exposure $11.10 exceeds max $10.00 for cluster 26AUG13"])
        f.record_rejection(["Position size too small"])
        f.execution_returned_nothing += 1
        f.placed += 1

        assert f.balances(), f.format()
        assert f.risk_rejected == 4
        assert "UNATTRIBUTED" not in f.format()

    def test_an_uncounted_path_is_reported(self):
        """The check that makes the counters trustworthy."""
        f = ExecutionFunnel(qualifying=6, placed=1)
        assert f.balances() is False
        assert "UNATTRIBUTED" in f.format()

    def test_rejection_reasons_aggregate_across_varying_dollar_amounts(self):
        """Limit messages embed live dollar figures. Counted verbatim, every
        rejection is its own unique reason and the counter never aggregates —
        which is exactly as useless as not counting at all."""
        f = ExecutionFunnel(qualifying=3)
        for dollars in (12.40, 11.10, 10.05):
            f.record_rejection([
                f"Cluster exposure ${dollars:.2f} exceeds max $10.00 for cluster 26AUG13"
            ])
        assert len(f.rejection_reasons) == 1, dict(f.rejection_reasons)
        assert f.rejection_reasons["Cluster exposure"] == 3

    def test_distinct_limits_stay_distinct(self):
        """Aggregation must not go so far that a cluster cap and a drawdown
        breaker read as the same event."""
        f = ExecutionFunnel(qualifying=2)
        f.record_rejection(["Cluster exposure $12.40 exceeds max $10.00"])
        f.record_rejection(["Drawdown 22% exceeds circuit breaker 20% — system stopped"])
        assert len(f.rejection_reasons) == 2, dict(f.rejection_reasons)

    def test_a_rejection_with_no_stated_reason_is_still_counted(self):
        f = ExecutionFunnel(qualifying=1)
        f.record_rejection([])
        assert f.risk_rejected == 1
        assert f.rejection_reasons["unspecified"] == 1


# --------------------------------------------------------------------------
# 2. Alert delivery is observable
# --------------------------------------------------------------------------

class TestAlertDelivery:
    def test_send_reports_failure_instead_of_swallowing_it(self, monkeypatch):
        import src.alerts as alerts

        def _boom(*a, **kw):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(alerts.httpx, "post", _boom)
        alerter = alerts.Alerter(token="t", chat_id="c")

        assert alerter.trade("KXHIGHNY-26AUG13-T92", "yes", 5, 44, 2.20, True) is False, (
            "a failed alert used to be indistinguishable from a delivered one"
        )

    def test_send_reports_success(self, monkeypatch):
        import src.alerts as alerts

        class _Resp:
            def raise_for_status(self):
                return None

        monkeypatch.setattr(alerts.httpx, "post", lambda *a, **kw: _Resp())
        alerter = alerts.Alerter(token="t", chat_id="c")
        assert alerter.trade("M", "yes", 5, 44, 2.20, True) is True

    def test_a_disabled_alerter_reports_undelivered_not_delivered(self):
        import src.alerts as alerts

        alerter = alerts.Alerter(token="", chat_id="")
        assert alerter.trade("M", "yes", 5, 44, 2.20, True) is False

    def test_the_funnel_flags_an_undelivered_trade_alert(self):
        f = ExecutionFunnel(qualifying=1, placed=1,
                            alerts_attempted=1, alerts_delivered=0)
        assert "UNDELIVERED" in f.format()
        f2 = ExecutionFunnel(qualifying=1, placed=1,
                             alerts_attempted=1, alerts_delivered=1)
        assert "UNDELIVERED" not in f2.format()


# --------------------------------------------------------------------------
# 3. The denominator
# --------------------------------------------------------------------------

class TestOpenMarketDenominator:
    """Production reported 32,074 open markets against a live universe of about
    5,100. `sync_markets` is the only writer of `Market.status` and it only
    writes rows the current fetch returned, so a market that closes keeps
    status='open' forever."""

    def _add(self, engine, market_id, days, status="open"):
        with get_session(engine) as s:
            s.add(Market(
                market_id=market_id, title=market_id, category="General",
                close_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days),
                status=status,
            ))
            s.commit()

    def test_past_close_markets_are_not_counted_as_open(self, engine):
        from src.maintenance.expire_markets import open_market_count

        self._add(engine, "LIVE-1", days=5)
        self._add(engine, "DEAD-1", days=-1)
        self._add(engine, "DEAD-2", days=-400)

        assert open_market_count(engine) == 1

    def test_the_sweep_flips_them_and_is_idempotent(self, engine):
        from src.maintenance.expire_markets import expire_closed_markets

        self._add(engine, "LIVE-1", days=5)
        self._add(engine, "DEAD-1", days=-1)

        assert expire_closed_markets(engine) == 1
        assert expire_closed_markets(engine) == 0, "second sweep must be a no-op"

        with get_session(engine) as s:
            status = {m.market_id: m.status for m in s.query(Market).all()}
        assert status == {"LIVE-1": "open", "DEAD-1": "closed"}

    def test_a_missing_close_date_cannot_reach_the_sweep(self):
        """Absence of a close date is not evidence of closure, and the sweep
        guards against it — but the guard is belt-and-braces, not the live
        path: the column is NOT NULL, so a row without one cannot exist. Pinned
        so that widening the column later surfaces the guard as load-bearing
        rather than leaving it as decoration.
        """
        from src.database import Base, load_all_models

        load_all_models()
        assert Base.metadata.tables["markets"].c["close_date"].nullable is False

    def test_the_scoring_funnel_uses_the_corrected_denominator(self, engine):
        from src.ev.funnel import ScoreFunnel
        from src.ev.scorer import score_all_markets

        self._add(engine, "LIVE-1", days=5)
        for i in range(20):
            self._add(engine, f"DEAD-{i}", days=-10)

        f = ScoreFunnel()
        score_all_markets(engine, funnel=f)
        assert f.open_markets == 1, (
            f"denominator {f.open_markets} counts markets that closed long ago; "
            "every real signal drowns in the stale-snapshot bucket"
        )
        assert f.balances(), f.format()
