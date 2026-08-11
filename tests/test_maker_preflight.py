"""The maker-enable checklist, digest degradation escalation, and day-7 labels.

Per the pinned principle, every guard here is shown FAILING when the thing it
protects is broken — the checklist blocking on an integer money path, the
escalation firing on day three and not day two, and a carried assumption being
labelled carried rather than passed off as measured.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from src.database import Base, get_session
from src.digest_health import (
    DEGRADED_DAYS_BEFORE_ALERT,
    degraded_sections,
    record_section,
)
from src.execution.day7 import MIN_PRINTS_TO_MEASURE, measure
from src.execution.preflight import (
    MakerBlocked,
    assert_maker_allowed,
    check_decimal_migration,
    maker_blockers,
    run_checklist,
)
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.models.settings import TradingSettings


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings())
        s.commit()
    return db_engine


# --------------------------------------------------------------------------
# 1. The checklist blocks, and blocks for the right reasons
# --------------------------------------------------------------------------

class TestMakerChecklist:
    def test_integer_money_path_blocks_maker(self, engine):
        """THE demonstrated failure. Trade.quantity is still Integer, so a
        fractional fill would be truncated rather than rejected."""
        passed, detail = check_decimal_migration(engine)
        assert passed is False
        assert "trades.quantity is INTEGER" in detail
        assert "truncated, not rejected" in detail

    def test_maker_enabled_with_blockers_raises(self, engine):
        """A config flag is not a decision."""
        with pytest.raises(MakerBlocked) as exc:
            assert_maker_allowed(engine, maker_enabled=True)
        assert "decimal_migration" in str(exc.value)

    def test_maker_disabled_never_raises(self, engine):
        assert_maker_allowed(engine, maker_enabled=False)      # no exception

    def test_licensing_is_an_attestation_and_defaults_to_blocked(self, engine):
        results = {r.item: r for r in run_checklist(engine)}
        licensing = results["forecast_licensing"]
        assert licensing.passed is False
        assert licensing.automatic is False
        assert "non-commercial" in licensing.detail

    def test_a_check_that_raises_counts_as_failed(self, engine, monkeypatch):
        """Treating an exception as a pass is the exact failure this file
        exists to prevent."""
        import src.execution.preflight as preflight

        def boom(engine):
            raise RuntimeError("inspector exploded")

        monkeypatch.setattr(preflight, "check_decimal_migration", boom)
        monkeypatch.setattr(
            preflight, "CHECKS", [("decimal_migration", boom, True)],
        )
        results = run_checklist(engine)
        assert results[0].passed is False
        assert "raised RuntimeError" in results[0].detail

    def test_weakening_the_paper_gate_blocks_maker(self, engine):
        """Maker execution must not become a route around the 50-trade gate."""
        with get_session(engine) as s:
            s.query(TradingSettings).first().paper_trades_before_live = 5
            s.commit()
        assert any("live_gate_untouched" in b for b in maker_blockers(engine))

    def test_checklist_is_not_vacuous(self, engine):
        """At least one item must be capable of passing, or the gate is just a
        permanent no and nobody would notice a broken check."""
        results = {r.item: r for r in run_checklist(engine)}
        assert results["live_gate_untouched"].passed is True


# --------------------------------------------------------------------------
# 2. Digest degradation escalates on day 3, not day 1
# --------------------------------------------------------------------------

class TestDigestDegradation:
    def _day(self, n):
        return dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc) + dt.timedelta(days=n)

    def test_healthy_section_never_escalates(self, engine):
        for day in range(5):
            alert, days = record_section(engine, "weather", True, now=self._day(day))
            assert alert is False and days == 0

    def test_one_bad_day_is_resilience_not_an_alert(self, engine):
        alert, days = record_section(engine, "weather", False, now=self._day(0))
        assert alert is False
        assert days == 1

    def test_escalates_exactly_on_the_third_consecutive_day(self, engine):
        alerts = []
        for day in range(4):
            alert, days = record_section(engine, "weather", False, now=self._day(day))
            alerts.append((day, alert, days))

        assert alerts[0][1] is False        # day 1 — resilience
        assert alerts[1][1] is False        # day 2
        assert alerts[2][1] is True         # day 3 — standing failure
        assert alerts[2][2] == DEGRADED_DAYS_BEFORE_ALERT
        assert alerts[3][1] is False        # day 4 — already alerted

    def test_repeated_cycles_in_one_day_count_once(self, engine):
        """The pipeline runs every five minutes. Counting cycles would escalate
        a transient blip within the hour, undoing the resilience entirely."""
        base = self._day(0)
        for minute in range(0, 60, 5):
            record_section(engine, "weather", False, now=base + dt.timedelta(minutes=minute))
        assert degraded_sections(engine) == [("weather", 1)]

    def test_recovery_resets_and_announces(self, engine):
        for day in range(3):
            record_section(engine, "weather", False, now=self._day(day))
        alert, days = record_section(engine, "weather", True, now=self._day(3))
        assert alert is True                # recovery worth saying out loud
        assert days == 0
        assert degraded_sections(engine) == []

    def test_sections_are_tracked_independently(self, engine):
        for day in range(3):
            record_section(engine, "weather", False, now=self._day(day))
            record_section(engine, "recorder", True, now=self._day(day))
        assert degraded_sections(engine) == [("weather", 3)]


# --------------------------------------------------------------------------
# 3. Day-7 labels measured vs carried
# --------------------------------------------------------------------------

class TestDay7Labelling:
    def _print(self, engine, ticker, ts_ms, price, category="Sports"):
        with get_session(engine) as s:
            if not s.query(Market).filter_by(market_id=ticker).first():
                s.add(Market(
                    market_id=ticker, title="t", category=category,
                    close_date=dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc),
                    status="open",
                ))
            s.add(OrderbookDeltaRaw(
                market_ticker=ticker, msg_type="trade", sid=1, seq=ts_ms,
                ts_ms=ts_ms,
                payload=json.dumps({"msg": {
                    "market_ticker": ticker, "ts_ms": ts_ms,
                    "yes_price_dollars": price, "count_fp": "5",
                    "taker_outcome_side": "no",
                }}),
            ))
            s.commit()

    def test_thin_data_is_labelled_carried_not_measured(self, engine):
        """A rate computed from a handful of prints is itself noise, and must
        not be dressed up as a measurement."""
        self._print(engine, "SPORTS-1", 1000, "0.44")
        stats = measure(engine)["Sports"]
        assert stats["rate_source"].startswith("CARRIED")
        assert stats["multi_level_rate"] == 0.10

    def test_sufficient_data_is_labelled_measured(self, engine):
        # One sweep per timestamp; every other sweep touches two levels.
        for i in range(MIN_PRINTS_TO_MEASURE + 20):
            self._print(engine, "SPORTS-1", 1000 + i, "0.44")
            if i % 2 == 0:
                self._print(engine, "SPORTS-1", 1000 + i, "0.43")
        stats = measure(engine)["Sports"]
        assert stats["rate_source"] == "MEASURED"
        assert stats["multi_level_rate"] > 0.0

    def test_categories_are_measured_separately(self, engine):
        self._print(engine, "SPORTS-1", 1000, "0.44", category="Sports")
        self._print(engine, "WX-1", 1000, "0.44", category="Climate and Weather")
        stats = measure(engine)
        assert set(stats) == {"Sports", "Climate and Weather"}

    def test_an_hour_of_data_projects_nothing(self, engine):
        """Caught by this test: one print in one hour extrapolated to a
        viable-looking N. Arithmetic is not evidence."""
        self._print(engine, "WX-1", 1000, "0.44", category="Climate and Weather")
        stats = measure(engine)["Climate and Weather"]
        assert stats["projectable"] is False
        assert stats["days_to_sample"] is None
        assert stats["recognised_fills_per_day"] is None

    def test_the_projection_guard_is_not_vacuous(self, engine):
        """With enough hours it DOES project — otherwise the guard would just
        be a permanent refusal that nobody would notice was broken."""
        from src.execution.day7 import MIN_HOURS_TO_PROJECT
        import datetime as dtm

        base = dtm.datetime(2026, 8, 1, tzinfo=dtm.timezone.utc)
        with get_session(engine) as s:
            s.add(Market(
                market_id="SP-1", title="t", category="Sports",
                close_date=dtm.datetime(2026, 12, 31, tzinfo=dtm.timezone.utc),
                status="open",
            ))
            for hour in range(MIN_HOURS_TO_PROJECT + 1):
                s.add(OrderbookDeltaRaw(
                    market_ticker="SP-1", msg_type="trade", sid=1, seq=hour,
                    ts_ms=1000 + hour,
                    payload=json.dumps({"msg": {
                        "market_ticker": "SP-1", "ts_ms": 1000 + hour,
                        "yes_price_dollars": "0.44", "count_fp": "5",
                        "taker_outcome_side": "no"}}),
                    received_at=base + dtm.timedelta(hours=hour),
                ))
            s.commit()
        stats = measure(engine)["Sports"]
        assert stats["projectable"] is True
        assert stats["days_to_sample"] is not None


# --------------------------------------------------------------------------
# 4. Deployed-commit reporting — "committed" is not "deployed"
# --------------------------------------------------------------------------

class TestDeployedCommitReporting:
    """Thirteen commits once sat unpushed while every report said 'shipped'.

    The guard is that the heartbeat states which commit is running, so drift is
    visible in the same message that claims health.
    """

    def test_reports_the_sha_when_running_in_actions(self, monkeypatch):
        from src.alerts import _deployed_line

        monkeypatch.setenv("GITHUB_SHA", "def6f98abcdef1234567890")
        monkeypatch.setenv("GITHUB_REF_NAME", "main")
        line = _deployed_line()
        assert "def6f98a" in line
        assert "main" in line

    def test_absence_of_sha_is_itself_the_signal(self, monkeypatch):
        """A local run must not look like a deployed one — that is exactly the
        confusion that let production run month-old code."""
        from src.alerts import _deployed_line

        monkeypatch.delenv("GITHUB_SHA", raising=False)
        assert "NOT a CI run" in _deployed_line()

    def test_heartbeat_carries_the_deploy_line(self, monkeypatch):
        from src.alerts import Alerter

        sent = []
        monkeypatch.setenv("GITHUB_SHA", "abc1234567")
        alerter = Alerter(token="t", chat_id="c")
        monkeypatch.setattr(alerter, "send", lambda text: sent.append(text))
        alerter.heartbeat(100.0, 11, 50)
        assert "🏷 deploy: abc12345" in sent[0]
