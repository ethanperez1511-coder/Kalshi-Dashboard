"""Retiring a deploy from the gate: dry run, token discipline, and the unwind.

Trade 1/50 was chosen by a formula valuing the bet at +0.85 when it was worth
+0.03. Retiring it is a named human decision — no invariant in the code can
know a formula was wrong — so the machinery around it has to be the machinery
for any destructive dispatch: report first, act only on an exact token, and
never let one action's token authorise another's.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.legacy_cutoff import gate_count
from src.maintenance.retire_deploy import (
    CONFIRM_TOKEN,
    execute_retirement,
    format_plan,
    plan_retirement,
)
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings
from src.models.trade import Trade

BAD_SHA = "e807f8dd0024530f41edf27088c6f4e4f883450b"
GOOD_SHA = "3ea612bfeedfacecafebabe0000000000000000"


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings(bankroll=100.0, paper_trade_count=0))
        s.commit()
    return db_engine


def _trade(engine, market_id, sha, side="no", price=92, qty=3):
    with get_session(engine) as s:
        s.add(Trade(
            market_id=market_id, side=side, action="buy", price=price,
            quantity=qty, p_model=0.0571, implied_prob=0.09, edge=-0.0329,
            traded_edge=0.0229, evaluated_price=price, net_ev=0.02,
            position_size_dollars=price * qty / 100.0, confidence=0.85,
            reasoning="t", is_paper=True, status="filled",
            model_name="WeatherModel", deploy_sha=sha, is_legacy=False,
        ))
        s.commit()


def _position(engine, market_id, side="no", entry=92, qty=3, mark_yes=6):
    with get_session(engine) as s:
        s.add(Position(
            market_id=market_id, side=side, entry_price=entry, quantity=qty,
            current_price=entry, status="open",
        ))
        if mark_yes is not None:
            s.add(PriceSnapshot(
                market_id=market_id, yes_bid=mark_yes - 1, yes_ask=mark_yes + 1,
                last_price=mark_yes, volume=500,
                timestamp=dt.datetime.now(dt.timezone.utc),
            ))
        s.commit()


# --------------------------------------------------------------------------
# 1. The dry run changes nothing
# --------------------------------------------------------------------------

class TestDryRun:
    def test_planning_writes_nothing(self, engine):
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        assert gate_count(engine) == 1

        plan = plan_retirement(engine, ["e807f8dd"])

        assert plan.trade_count == 1
        assert (plan.gate_before, plan.gate_after) == (1, 0)
        assert gate_count(engine) == 1, "a plan is not an action"
        assert "DRY RUN" in format_plan(plan)

    def test_the_report_names_the_trade_and_the_gate_delta(self, engine):
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        text = format_plan(plan_retirement(engine, ["e807f8dd"]))
        assert "KXHIGHCHI-26AUG13-T76" in text
        assert "1 -> 0" in text
        assert "traded_edge=+0.0229" in text


# --------------------------------------------------------------------------
# 2. Selection. An empty selector is a no-op, never a wildcard.
# --------------------------------------------------------------------------

class TestSelection:
    def test_only_the_named_deploy_is_retired(self, engine):
        _trade(engine, "BAD-1", BAD_SHA)
        _trade(engine, "GOOD-1", GOOD_SHA)

        plan = plan_retirement(engine, ["e807f8dd"])
        execute_retirement(engine, plan)

        assert gate_count(engine) == 1
        with get_session(engine) as s:
            legacy = {t.market_id: t.is_legacy for t in s.query(Trade).all()}
        assert legacy == {"BAD-1": True, "GOOD-1": False}

    @pytest.mark.parametrize("shas", [[], [""], ["   "], ["", "  "]])
    def test_a_blank_selector_retires_nothing(self, engine, shas):
        """The failure that would void the whole gate on a blank workflow input.
        A selector that means "everything" when empty is the same class of bug
        as `rm -rf $UNSET/`."""
        _trade(engine, "GOOD-1", GOOD_SHA)

        plan = plan_retirement(engine, shas)
        execute_retirement(engine, plan)

        assert plan.trade_count == 0
        assert gate_count(engine) == 1

    def test_a_prefix_from_the_deployment_line_is_enough(self, engine):
        _trade(engine, "BAD-1", BAD_SHA)
        assert plan_retirement(engine, ["e807f8dd"]).trade_count == 1
        assert plan_retirement(engine, [BAD_SHA]).trade_count == 1
        assert plan_retirement(engine, ["e807f8de"]).trade_count == 0


# --------------------------------------------------------------------------
# 3. The unwind
# --------------------------------------------------------------------------

class TestUnwind:
    def test_an_open_position_is_closed_at_its_mark(self, engine):
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        _position(engine, "KXHIGHCHI-26AUG13-T76", side="no", entry=92, mark_yes=6)

        plan = plan_retirement(engine, ["e807f8dd"])
        assert plan.open_positions[0]["mark"] == 94   # NO mark = 100 - yes 6

        result = execute_retirement(engine, plan)

        assert result["closed"][0]["status"] == "closed"
        with get_session(engine) as s:
            assert s.query(Position).one().status == "closed"

    def test_the_closing_trade_does_not_become_gate_evidence(self, engine):
        """The close writes its own trade row carrying the CURRENT deploy SHA.
        Left alone, unwinding discredited evidence becomes evidence."""
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        _position(engine, "KXHIGHCHI-26AUG13-T76", mark_yes=6)

        execute_retirement(engine, plan_retirement(engine, ["e807f8dd"]))

        assert gate_count(engine) == 0
        with get_session(engine) as s:
            assert all(t.is_legacy for t in s.query(Trade).all())

    def test_a_position_with_no_mark_is_left_open_not_closed_at_a_guess(self, engine):
        """No price feed, no honest exit. Closing at a made-up number would
        write a fabricated realized PnL straight into the record."""
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        _position(engine, "KXHIGHCHI-26AUG13-T76", mark_yes=None)

        plan = plan_retirement(engine, ["e807f8dd"])
        assert plan.unmarked == ["KXHIGHCHI-26AUG13-T76"]
        assert "NO MARK AVAILABLE" in format_plan(plan)

        result = execute_retirement(engine, plan)
        assert result["closed"][0]["status"] == "no_mark_left_open"
        with get_session(engine) as s:
            assert s.query(Position).one().status == "open"

    def test_history_is_kept(self, engine):
        _trade(engine, "KXHIGHCHI-26AUG13-T76", BAD_SHA)
        execute_retirement(engine, plan_retirement(engine, ["e807f8dd"]))
        with get_session(engine) as s:
            row = s.query(Trade).filter_by(market_id="KXHIGHCHI-26AUG13-T76").first()
            kept = (row.is_legacy, row.price, row.quantity, row.traded_edge)
        assert kept == (True, 92, 3, 0.0229)

    def test_the_gate_counter_follows_the_rows(self, engine):
        _trade(engine, "BAD-1", BAD_SHA)
        with get_session(engine) as s:
            s.query(TradingSettings).first().paper_trade_count = 1
            s.commit()

        execute_retirement(engine, plan_retirement(engine, ["e807f8dd"]))

        with get_session(engine) as s:
            assert s.query(TradingSettings).first().paper_trade_count == 0

    def test_retiring_twice_is_idempotent(self, engine):
        _trade(engine, "BAD-1", BAD_SHA)
        execute_retirement(engine, plan_retirement(engine, ["e807f8dd"]))
        again = execute_retirement(engine, plan_retirement(engine, ["e807f8dd"]))
        assert again["retired"] == 0
        assert gate_count(engine) == 0


# --------------------------------------------------------------------------
# 4. Token discipline
# --------------------------------------------------------------------------

class TestTokens:
    def _run(self, monkeypatch, engine, argv):
        import src.maintenance.__main__ as entry

        monkeypatch.setattr(entry, "require_production_database", lambda url: None)
        monkeypatch.setattr(entry, "get_engine", lambda url: engine)
        monkeypatch.setattr(entry, "verify_or_migrate", lambda *a, **k: None)
        return entry.main(argv)

    def test_no_token_is_a_dry_run(self, engine, monkeypatch):
        _trade(engine, "BAD-1", BAD_SHA)
        assert self._run(monkeypatch, engine, ["--retire-sha", "e807f8dd"]) == 0
        assert gate_count(engine) == 1, "a report must not be an action"

    def test_a_near_miss_token_refuses_loudly(self, engine, monkeypatch):
        _trade(engine, "BAD-1", BAD_SHA)
        code = self._run(monkeypatch, engine,
                         ["--retire-sha", "e807f8dd", "--confirm", "RETIRE-DEPLOY-SHAS"])
        assert code == 2
        assert gate_count(engine) == 1

    def test_the_unwind_token_cannot_authorise_a_gate_reset(self, engine, monkeypatch):
        """Two destructive actions must not share a key."""
        _trade(engine, "BAD-1", BAD_SHA)
        code = self._run(monkeypatch, engine,
                         ["--retire-sha", "e807f8dd",
                          "--confirm", "CLOSE-LEGACY-POSITIONS"])
        assert code == 2
        assert gate_count(engine) == 1

    def test_the_exact_token_executes(self, engine, monkeypatch):
        _trade(engine, "BAD-1", BAD_SHA)
        assert self._run(monkeypatch, engine,
                         ["--retire-sha", "e807f8dd", "--confirm", CONFIRM_TOKEN]) == 0
        assert gate_count(engine) == 0
