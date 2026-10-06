"""The fee schedule on the execution path (L27: test the path, not the unit)."""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import get_session
from src.models.trade import Trade
from src.trading.fee_schedule import FeeBook
from tests.test_shadow_wiring import MARKET, NOW, _Alerter, _qualifying, engine  # noqa: F401


def _book(engine, answer):
    alerts = []

    def fetch(series):
        if isinstance(answer, Exception):
            raise answer
        return answer

    return FeeBook(engine, fetch=fetch, alert=alerts.append, now=NOW), alerts


def _execute(engine, monkeypatch, book, qualifying=None):
    import src.run_trading as rt

    monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", False)
    return rt.execute_qualifying(
        engine, qualifying or _qualifying(), _Alerter(), now=NOW, fee_book=book,
    )


def _trades(engine):
    with get_session(engine) as s:
        return s.query(Trade.id).all()


def test_a_refused_series_is_not_traded_and_says_why(engine, monkeypatch):
    book, alerts = _book(engine, RuntimeError("503"))          # never seen: refused
    funnel, placed = _execute(engine, monkeypatch, book)
    assert placed == 0 and _trades(engine) == []
    assert funnel.fee_refused == 1 and funnel.balances()
    assert len(alerts) == 1


def test_todays_schedule_trades_exactly_as_before(engine, monkeypatch):
    book, alerts = _book(engine, ("quadratic", 1))
    _, placed = _execute(engine, monkeypatch, book)
    assert placed == 1 and alerts == []


def test_a_higher_fee_that_erases_the_edge_is_refused(engine, monkeypatch):
    """net_ev 0.002/contract at 46c: a 3x multiplier adds ~$0.04 of fee."""
    opp = dict(_qualifying()[0], traded_net_ev=0.002, evaluated_price=46)
    book, _ = _book(engine, ("quadratic", 3))
    funnel, placed = _execute(engine, monkeypatch, book, [opp])
    assert placed == 0
    assert any("after-fee" in reason for reason in funnel.fee_reasons)


def test_the_paper_fill_is_charged_the_series_true_rate(engine, monkeypatch):
    opp = dict(_qualifying()[0], traded_net_ev=0.5, evaluated_price=46)
    book, _ = _book(engine, ("quadratic", 2))
    _execute(engine, monkeypatch, book, [opp])
    with get_session(engine) as s:
        ((fee, qty, price),) = s.query(Trade.entry_fee, Trade.quantity, Trade.price).all()
    from src.trading.fees import kalshi_fee

    assert fee == pytest.approx(kalshi_fee(qty, price, 0.14))
    assert fee > kalshi_fee(qty, price)


def test_run_pipeline_builds_a_fee_book_for_execution():
    """Production must always pass one: without it the hardcoded rate is
    silently back in charge."""
    import inspect

    import src.run_trading as rt

    source = inspect.getsource(rt._run_pipeline_locked)
    assert "FeeBook(" in source and "fee_book=" in source


def test_a_no_call_is_judged_on_the_no_side_ev(engine, monkeypatch):
    """`net_ev` is the YES side's even on a NO call. A NO trade with a healthy
    NO edge must not be refused because the YES side is negative."""
    opp = dict(_qualifying()[0], recommended_side="no", net_ev=-0.30,
               traded_net_ev=0.25, evaluated_price=56)
    book, _ = _book(engine, ("quadratic", 2))
    funnel, _ = _execute(engine, monkeypatch, book, [opp])
    assert funnel.fee_refused == 0
