"""Reconcile and unwind: report first, act only on an exact token.

Per the pinned principle each guard is shown failing: a near-miss token refuses
rather than dry-running, a position with no mark is flagged rather than closed
at an invented price, and inferred attribution is labelled inferred rather than
passed off as recorded.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.maintenance.legacy_positions import (
    CONFIRM_TOKEN,
    execute_closures,
    format_report,
    infer_model,
    reconcile,
)
from src.maintenance.__main__ import main as maintenance_main
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings
from src.models.trade import Trade


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings())
        s.commit()
    return db_engine


def _position(engine, market, side="no", entry=96, qty=3, reasoning="Polymarket match",
              model=None, mark=95):
    with get_session(engine) as s:
        s.add(Trade(
            market_id=market, side=side, action="buy", price=entry, quantity=qty,
            p_model=0.004, implied_prob=0.04, edge=0.03, net_ev=0.01,
            position_size_dollars=entry * qty / 100.0, confidence=0.8,
            reasoning=reasoning, is_paper=True, status="filled",
            model_name=model, is_legacy=True,
        ))
        s.add(Position(market_id=market, side=side, entry_price=entry,
                       quantity=qty, current_price=entry, status="open"))
        if mark is not None:
            s.add(PriceSnapshot(market_id=market, yes_bid=mark - 1, yes_ask=mark + 1,
                                last_price=mark, volume=100))
        s.commit()


class TestAttribution:
    def test_recorded_beats_inferred(self):
        assert infer_model("Polymarket match", "WeatherModel") == ("WeatherModel", "recorded")

    def test_polymarket_is_inferred_from_reasoning(self):
        assert infer_model('Polymarket match (sim=1.00)', None) == ("PolymarketModel", "inferred")

    def test_parlay_is_inferred_as_sports(self):
        assert infer_model("Parlay 4 legs: spread(38%,ext)", None)[0] == "SportsOddsModel"

    def test_unattributable_is_unknown_not_bucketed(self):
        """Guessing an attribution would put a position in the wrong action."""
        assert infer_model("something else entirely", None) == ("unknown", "inferred")


class TestReconcile:
    def test_polymarket_positions_are_planned_for_closure(self, engine):
        _position(engine, "KXNEXTISRAELPM-45JAN01-NBEN")
        report = reconcile(engine)
        assert [p.market_id for p in report.to_close] == ["KXNEXTISRAELPM-45JAN01-NBEN"]
        assert "understates YES" in report.to_close[0].reason

    def test_sports_positions_are_held_not_closed(self, engine):
        """Flag, do not decide — out of scope for this unwind."""
        _position(engine, "SPORTS-1", reasoning="Parlay 4 legs: spread(38%,ext)")
        report = reconcile(engine)
        assert report.to_close == []
        assert report.positions[0].action == "hold"

    def test_unknown_attribution_is_flagged_for_review(self, engine):
        _position(engine, "MYSTERY-1", reasoning="???")
        report = reconcile(engine)
        assert [p.market_id for p in report.to_flag] == ["MYSTERY-1"]

    def test_a_position_with_no_mark_is_flagged_not_closed(self, engine):
        """Closing at an invented price books fabricated PnL that then feeds
        calibration — worse than leaving it open."""
        _position(engine, "NOMARK-1", mark=None)
        report = reconcile(engine)
        assert report.to_close == []
        assert "NO current mark" in report.to_flag[0].reason

    def test_reconcile_writes_nothing(self, engine):
        _position(engine, "KX-1")
        reconcile(engine)
        with get_session(engine) as s:
            assert s.query(Position).filter_by(status="open").count() == 1

    def test_attribution_source_is_reported(self, engine):
        _position(engine, "KX-1")
        assert reconcile(engine).attribution_sources == {"inferred": 1}


class TestExecute:
    def test_closes_at_the_current_mark_and_books_pnl(self, engine):
        _position(engine, "KX-1", side="no", entry=96, qty=3, mark=95)
        report = reconcile(engine)
        results = execute_closures(engine, report)

        assert results[0]["status"] == "closed"
        with get_session(engine) as s:
            assert s.query(Position).filter_by(status="open").count() == 0
            closed = s.query(Trade).filter_by(market_id="KX-1").first()
            assert closed.status == "closed"
            assert closed.realized_pnl is not None

    def test_closed_rows_stay_legacy(self, engine):
        """The unwind must not feed the gate or the calibration fit."""
        _position(engine, "KX-1")
        execute_closures(engine, reconcile(engine))
        with get_session(engine) as s:
            assert s.query(Trade).filter_by(market_id="KX-1").first().is_legacy is True
            assert s.query(TradingSettings).first().paper_trade_count == 0

    def test_held_positions_are_untouched(self, engine):
        _position(engine, "SPORTS-1", reasoning="Parlay 4 legs")
        execute_closures(engine, reconcile(engine))
        with get_session(engine) as s:
            assert s.query(Position).filter_by(market_id="SPORTS-1", status="open").count() == 1


class TestConfirmationGate:
    def _point_at(self, engine, monkeypatch, tmp_path):
        db = tmp_path / "m.db"
        import shutil
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
        from src.database import get_engine, ensure_schema
        target = get_engine(f"sqlite:///{db}")
        ensure_schema(target)
        with get_session(target) as s:
            s.add(TradingSettings())
            s.commit()
        _position(target, "KX-1")
        return target

    def test_no_token_is_a_dry_run(self, engine, monkeypatch, tmp_path, capsys):
        target = self._point_at(engine, monkeypatch, tmp_path)
        assert maintenance_main([]) == 0
        assert "DRY RUN" in capsys.readouterr().out
        with get_session(target) as s:
            assert s.query(Position).filter_by(status="open").count() == 1

    def test_a_near_miss_token_refuses_rather_than_dry_running(
        self, engine, monkeypatch, tmp_path,
    ):
        """A typo on a destructive action must not silently become a dry run
        the operator mistakes for a completed one."""
        target = self._point_at(engine, monkeypatch, tmp_path)
        assert maintenance_main(["--confirm", "close-legacy-positions"]) == 2
        with get_session(target) as s:
            assert s.query(Position).filter_by(status="open").count() == 1

    def test_the_exact_token_executes(self, engine, monkeypatch, tmp_path):
        target = self._point_at(engine, monkeypatch, tmp_path)
        assert maintenance_main(["--confirm", CONFIRM_TOKEN]) == 0
        with get_session(target) as s:
            assert s.query(Position).filter_by(status="open").count() == 0


def test_report_labels_dry_run_explicitly(engine):
    _position(engine, "KX-1")
    text = format_report(reconcile(engine), executed=None)
    assert "DRY RUN — nothing was changed." in text
    assert "PLAN: close 1" in text
