"""Read-only paper-trading summary: every paper trade, and the gate-counted
subset (legacy rows excluded), with the bankroll reconciled against the
realized P&L that produced it."""
from __future__ import annotations

import pytest

from src.database import Base, get_session
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.trade import Trade


def _trade(market, side, price, qty, status, pnl=None, legacy=False, fee=0.0):
    return Trade(
        market_id=market, side=side, action="buy", price=price, quantity=qty,
        p_model=0.5, implied_prob=price / 100, edge=0.05, net_ev=0.02,
        position_size_dollars=price * qty / 100, confidence=0.8, reasoning="t",
        is_paper=True, status=status, realized_pnl=pnl, entry_fee=fee,
        model_name="WeatherModel", deploy_sha="bad" if legacy else "good",
        is_legacy=legacy,
    )


@pytest.fixture
def book(db_engine):
    import src.models  # noqa: F401
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        # bankroll = 100 + sum of every settled trade's realized P&L
        s.add(TradingSettings(bankroll=100 + 3.0 - 2.0 + 1.5 - 4.0, peak_bankroll=104.0))
        s.add_all([
            _trade("KXHIGHCHI-26OCT01-T70", "no", 80, 5, "closed", pnl=3.0, fee=0.1),
            _trade("KXHIGHCHI-26OCT02-T71", "yes", 40, 5, "closed", pnl=-2.0, fee=0.1),
            _trade("KXHIGHNY-26OCT03-T60", "no", 70, 5, "closed", pnl=1.5, fee=0.1),
            # the legacy deploy: settled loss, counts in "all", not in "gate"
            _trade("KXHIGHMIA-26OCT04-T88", "yes", 50, 8, "closed", pnl=-4.0, legacy=True),
            _trade("KXHIGHNY-26OCT14-T62", "no", 60, 10, "filled"),
            _trade("KXHIGHNY-26OCT14-T64", "yes", 30, 10, "cancelled"),
        ])
        s.add(Position(market_id="KXHIGHNY-26OCT14-T62", side="no", entry_price=60,
                       quantity=10, current_price=75, status="open"))
        s.commit()
    return db_engine


def test_all_trades_and_gate_scopes(book):
    from src.maintenance.paper_summary import summarize
    r = summarize(book)

    a, g = r.scopes["all"], r.scopes["gate"]
    assert (a.total, a.settled, a.open, a.other) == (6, 4, 1, 1)
    assert (g.total, g.settled, g.open) == (5, 3, 1)
    assert (a.wins, a.losses) == (2, 2) and a.win_rate == pytest.approx(0.5)
    assert (g.wins, g.losses) == (2, 1)
    assert a.realized == pytest.approx(-1.5) and g.realized == pytest.approx(2.5)
    assert a.avg_win == pytest.approx(2.25) and a.avg_loss == pytest.approx(-3.0)
    assert a.largest_win[0] == pytest.approx(3.0) and a.largest_loss[0] == pytest.approx(-4.0)
    assert a.largest_loss[1] == "KXHIGHMIA-26OCT04-T88"


def test_breakdowns_by_side_and_city(book):
    from src.maintenance.paper_summary import summarize
    a = summarize(book).scopes["all"]
    assert a.by_side["no"] == (2, pytest.approx(4.5))
    assert a.by_side["yes"] == (2, pytest.approx(-6.0))
    assert a.by_city["Chicago"][1] == pytest.approx(1.0)
    assert a.by_city["New York"][1] == pytest.approx(1.5)
    assert a.by_city["Miami"][1] == pytest.approx(-4.0)


def test_bankroll_reconciles_and_open_positions_are_at_cost(book):
    from src.maintenance.paper_summary import START_BANKROLL, summarize
    r = summarize(book)
    assert START_BANKROLL == 100.0
    assert r.bankroll == pytest.approx(98.5)
    assert r.reconciliation_gap == pytest.approx(0.0)
    assert r.open_cost == pytest.approx(6.0)
    assert r.open_market_value == pytest.approx(7.5)


def test_report_text_states_both_figures_and_is_read_only(book):
    from src.maintenance.paper_summary import format_summary, summarize
    before = _snapshot(book)
    text = format_summary(summarize(book))
    assert _snapshot(book) == before
    assert "ALL paper trades" in text and "GATE-COUNTED" in text
    assert "-1.50" in text and "+2.50" in text
    assert "at cost" in text


def _snapshot(engine):
    with get_session(engine) as s:
        return (s.query(TradingSettings.bankroll).scalar(),
                sorted((t.id, t.status, t.realized_pnl) for t in s.query(Trade).all()))


def test_every_traded_weather_series_has_a_city():
    from src.maintenance.paper_summary import CITIES
    from src.weather.stations import STATIONS
    assert set(STATIONS) <= set(CITIES)


def test_it_runs_as_the_workflow_runs_it(book, monkeypatch, capsys):
    from tests.test_maintenance_entrypoint import run_module
    code, out = run_module(["--paper-summary"], book, monkeypatch, capsys)
    assert code == 0, out
    assert "GATE-COUNTED" in out


def test_the_workflow_offers_it():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/maintenance.yml"))
    assert "paper-summary" in wf[True]["workflow_dispatch"]["inputs"]["action"]["options"]
    run = [s.get("run", "") for s in wf["jobs"]["run"]["steps"]]
    assert any("--paper-summary" in r for r in run)


def test_a_second_fill_on_a_settled_market_is_not_counted_open(book):
    from src.maintenance.paper_summary import summarize
    with get_session(book) as s:
        s.add(_trade("KXHIGHCHI-26OCT01-T70", "no", 80, 5, "filled"))   # position closed
        s.commit()
    a = summarize(book).scopes["all"]
    assert (a.open, a.stranded) == (1, 1)
