"""Preflight is re-checked continuously, not once at first addition.

A gate that runs once is a gate that was true once. The decimal migration, the
capture-beats-taker check and the live-gate check can all stop being true after
a series is added, and a first-addition-only gate would never look again.

A blocker forces maker OFF for every series — it does not abort the trade. The
trade still goes out as taker, which is the conservative execution style and
the thing maker was being promoted away from. Refusing to trade at all would
turn a maker misconfiguration into a full paper-trading outage, which is
strictly worse and not what fail-closed means here.
"""
from __future__ import annotations

import pytest

from src.database import Base, get_session
from src.models.settings import TradingSettings


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(TradingSettings(bankroll=100.0, paper_trade_count=0, mode="paper"))
        session.commit()
    return db_engine


@pytest.fixture
def maker_on(reload_config):
    return reload_config(
        "src.execution.allowlist",
        TRADING_MAKER_ENABLED="true",
        TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
    )


class TestBlockersForceTakerEverywhere:
    def test_a_blocker_downgrades_an_enabled_series(self, engine, maker_on, monkeypatch):
        monkeypatch.setattr(
            maker_on, "_maker_blockers",
            lambda eng: ["decimal migration: not applied"],
        )

        assert maker_on.resolve_order_type(engine, "KXHIGHLAX-26AUG19-T83") == "taker"

    def test_a_clean_checklist_leaves_maker_on(self, engine, maker_on, monkeypatch):
        monkeypatch.setattr(maker_on, "_maker_blockers", lambda eng: [])

        assert maker_on.resolve_order_type(engine, "KXHIGHLAX-26AUG19-T83") == "maker"

    def test_the_blocker_is_named_not_merely_applied(
        self, engine, maker_on, monkeypatch, caplog,
    ):
        monkeypatch.setattr(
            maker_on, "_maker_blockers",
            lambda eng: ["capture beats taker: no evidence"],
        )

        with caplog.at_level("ERROR"):
            maker_on.resolve_order_type(engine, "KXHIGHLAX-26AUG19-T83")

        assert "capture beats taker" in caplog.text

    def test_one_blocker_disables_every_series_not_just_the_failing_one(
        self, engine, reload_config, monkeypatch,
    ):
        mod = reload_config(
            "src.execution.allowlist",
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX,KXHIGHCHI,KXHIGHNY",
        )
        monkeypatch.setattr(mod, "_maker_blockers", lambda eng: ["live gate: moved"])

        for series in ("KXHIGHLAX", "KXHIGHCHI", "KXHIGHNY"):
            assert mod.resolve_order_type(engine, f"{series}-26AUG19-T90") == "taker"


class TestItCostsNothingWhileMakerIsOff:
    def test_no_checklist_runs_when_the_list_is_empty(self, engine, reload_config):
        mod = reload_config("src.execution.allowlist")
        calls = []

        original = mod._maker_blockers
        mod._maker_blockers = lambda eng: calls.append(1) or []
        try:
            assert mod.resolve_order_type(engine, "KXHIGHLAX-26AUG19-T83") == "taker"
        finally:
            mod._maker_blockers = original

        assert calls == [], "checklist ran while maker was off everywhere"

    def test_the_checklist_is_evaluated_once_per_process(
        self, engine, maker_on, monkeypatch,
    ):
        """One cycle is one process, so once per process is once per cycle."""
        calls = []
        monkeypatch.setattr(
            maker_on, "_run_checklist", lambda eng: calls.append(1) or [],
        )
        maker_on._reset_blocker_cache()

        for _ in range(5):
            maker_on.resolve_order_type(engine, "KXHIGHLAX-26AUG19-T83")

        assert len(calls) == 1
