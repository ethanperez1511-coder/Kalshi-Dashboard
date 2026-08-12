"""Retry shapes and stage budgets.

A cycle was killed by the 8-minute job cap because a 422 from Polymarket's
pagination was treated as a retryable failure: the exception escaped
get_markets, the cache was never set, and every subsequent market restarted the
whole walk from offset 0. An infinite loop wearing a retry's clothes.

Two invariants come out of that, and both are tested here rather than asserted:
a deterministic 4xx is never retried, and no single stage can consume the whole
cycle.
"""
from __future__ import annotations

import time

import pytest

from src.deadline import Deadline
from src.modeling.polymarket_api import PolymarketClient


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


def _row(i):
    import json
    return {
        "question": f"Will thing {i} happen?",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.6", "0.4"]),
        "volumeNum": 100000,
        "conditionId": f"0x{i:04d}",
        "endDate": "2026-12-31T00:00:00Z",
    }


@pytest.fixture
def fake_http(monkeypatch):
    calls = {"offsets": []}

    def make(responder):
        def fake_get(url, params=None, timeout=None):
            calls["offsets"].append(params["offset"])
            return responder(params["offset"])

        monkeypatch.setattr("src.modeling.polymarket_api.httpx.get", fake_get)
        return calls

    return make


class TestPaginationTerminates:
    def test_a_422_ends_pagination_and_keeps_what_was_fetched(self, fake_http):
        """THE production bug. Gamma answers 422 past a few thousand rows."""
        def responder(offset):
            if offset >= 300:
                return _Resp(422)
            return _Resp(200, [_row(offset + i) for i in range(100)])

        calls = fake_http(responder)
        markets = PolymarketClient(max_markets=5000).get_markets()

        assert len(markets) == 300
        assert calls["offsets"] == [0, 100, 200, 300]      # stops at the 422

    def test_the_walk_never_restarts_from_zero(self, fake_http):
        """The infinite loop: the cache was left unset on failure, so every
        caller re-walked from the beginning."""
        def responder(offset):
            return _Resp(422) if offset >= 200 else _Resp(200, [_row(offset)])

        calls = fake_http(responder)
        client = PolymarketClient(max_markets=5000)
        client.get_markets()
        first = list(calls["offsets"])
        client.get_markets()
        client.get_markets()

        assert calls["offsets"] == first, "re-walked after a 4xx"
        assert calls["offsets"].count(0) == 1

    def test_an_empty_result_is_cached_too(self, fake_http):
        """Even a total failure must not re-walk."""
        calls = fake_http(lambda offset: _Resp(422))
        client = PolymarketClient()
        assert client.get_markets() == []
        client.get_markets()
        assert len(calls["offsets"]) == 1

    def test_other_4xx_are_not_retried_either(self, fake_http):
        """4xx means the request is wrong; an identical request fails
        identically. Retrying is guaranteed waste."""
        calls = fake_http(lambda offset: _Resp(400))
        PolymarketClient().get_markets()
        assert len(calls["offsets"]) == 1

    def test_5xx_stops_rather_than_spinning(self, fake_http):
        calls = fake_http(lambda offset: _Resp(503))
        PolymarketClient().get_markets()
        assert len(calls["offsets"]) == 1

    def test_a_transport_error_keeps_partial_results(self, fake_http, monkeypatch):
        import httpx

        state = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            state["n"] += 1
            if state["n"] > 2:
                raise httpx.ConnectError("boom")
            return _Resp(200, [_row(i) for i in range(100)])

        monkeypatch.setattr("src.modeling.polymarket_api.httpx.get", fake_get)
        markets = PolymarketClient(max_markets=5000).get_markets()
        assert len(markets) == 200

    def test_pagination_is_hard_capped(self, fake_http):
        """Terminates even if the API never says stop."""
        calls = fake_http(lambda offset: _Resp(200, [_row(offset)]))
        PolymarketClient(max_markets=10**9).get_markets()
        assert len(calls["offsets"]) == PolymarketClient.MAX_PAGES


class TestDeadline:
    def test_an_unexpired_deadline_does_not_fire(self):
        assert Deadline(60, "x").expired() is False

    def test_an_expired_deadline_fires_and_records_it(self):
        d = Deadline(0, "ingest")
        time.sleep(0.01)
        assert d.expired() is True
        assert d.exceeded is True

    def test_none_is_unbounded(self):
        d = Deadline.none("x")
        assert d.expired() is False
        assert d.remaining() == float("inf")

    def test_it_only_logs_once(self, caplog):
        import logging

        d = Deadline(0, "ingest")
        time.sleep(0.01)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                d.expired()
        assert caplog.text.count("exceeded its") == 1


class TestStageBudgetsStopWork:
    def test_scoring_stops_early_and_the_cycle_continues(self, db_engine):
        """One slow stage must not destroy settlement, the digest and the tick."""
        import datetime as dtm

        from src.database import Base, get_session
        from src.ev.scorer import score_all_markets
        from src.models.market import Market
        from src.models.price import PriceSnapshot

        Base.metadata.create_all(db_engine)
        now = dtm.datetime.now(dtm.timezone.utc)
        with get_session(db_engine) as s:
            for i in range(200):
                s.add(Market(
                    market_id=f"M-{i}", title=f"t{i}", category="General",
                    close_date=now + dtm.timedelta(days=2), status="open",
                ))
                s.add(PriceSnapshot(
                    market_id=f"M-{i}", yes_bid=44, yes_ask=46,
                    last_price=45, volume=500, timestamp=now,
                ))
            s.commit()

        expired = Deadline(0, "scoring")
        time.sleep(0.01)
        results = score_all_markets(db_engine, deadline=expired)

        assert expired.exceeded is True
        assert isinstance(results, list)      # returns cleanly, does not raise

    def test_no_deadline_means_no_early_stop(self, db_engine):
        """The budget must not be a permanent truncation."""
        from src.database import Base
        from src.ev.scorer import score_all_markets

        Base.metadata.create_all(db_engine)
        assert score_all_markets(db_engine, deadline=Deadline.none("x")) == []
