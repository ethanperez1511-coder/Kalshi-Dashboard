"""Shadow maker rebuild (rulings 2026-10-06). One test per defect, red first.

The 28 production rows recorded 0 fills, and that was by construction rather
than evidence. Six defects, each driven through the PRODUCTION path
(`execute_qualifying` with the real simulator), then resolved as the next
cycle resolves it:

  1. judged at placement: the rest window was in the future, so its tape could
     not exist yet and every order came out `unfilled`
  2. rested at the taker price we had just paid, and walked UP from there, so
     it was never a maker order
  3. category never reached the row, so every row was "unknown"
  4a. gaps were matched to the order's own ticker, but `seq` is per
      SUBSCRIPTION, shared by every market on it, so a gap revealed by
      another market's message was missed
  4b. a gap is detected at the NEXT message, which can arrive after the
      window ends, and that was missed too
  5. the maker leg was charged the TAKER fee
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

import src.execution.shadow as shadow
from src.database import get_session
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap
from src.models.shadow import ShadowMakerOrder
from src.trading.fee_schedule import FeeBook
from tests.test_shadow_wiring import MARKET, NOW, _Alerter, engine  # noqa: F401

NOW_MS = int(NOW.timestamp() * 1000)
OTHER = "KXHIGHCHI-26AUG18-T80"


def _opp(**kw):
    base = {
        "market_id": MARKET, "p_model": 0.70, "implied_prob": 0.45,
        "edge": 0.25, "net_ev": 0.2, "traded_net_ev": 0.2, "recommended_side": "yes",
        "confidence": 0.8, "reasoning": "t", "model_name": "WeatherModel",
        "yes_bid": 44, "yes_ask": 46, "category": "Weather", "evaluated_price": 46,
    }
    base.update(kw)
    return base


def _row(engine, ticker, msg_type, at, sid=1, seq=1, payload=None):
    with get_session(engine) as s:
        s.add(OrderbookDeltaRaw(
            market_ticker=ticker, msg_type=msg_type, sid=sid, seq=seq,
            ts_ms=int(at.timestamp() * 1000), payload=payload or "{}", received_at=at,
        ))
        s.commit()


def _print(engine, at, yes_price="0.4300", count="50", taker_outcome="no"):
    """A NO-taker print at 43c: it consumes resting YES bids at 44c and above."""
    _row(engine, MARKET, "trade", at, sid=2, payload=json.dumps({"msg": {
        "ts_ms": int(at.timestamp() * 1000), "yes_price_dollars": yes_price,
        "no_price_dollars": str(Decimal("1") - Decimal(yes_price)),
        "count_fp": count, "taker_outcome_side": taker_outcome, "is_block_trade": False,
    }}))


def _covered(engine):
    """The recorder was subscribed before the order and alive after it."""
    _row(engine, MARKET, "snapshot", NOW - dt.timedelta(minutes=10))
    _row(engine, OTHER, "delta", NOW + dt.timedelta(minutes=5))


def _place(engine, monkeypatch, **opp):
    import src.run_trading as rt

    monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", True)
    book = FeeBook(engine, fetch=lambda s: ("quadratic", 1), alert=lambda m: None, now=NOW)
    rt.execute_qualifying(engine, [_opp(**opp)], _Alerter(), now=NOW, fee_book=book)


def _resolve(engine, minutes_later=10):
    """What the next cycle does. Absent on the old code, where nothing ever
    revisits a row; that is defect 1."""
    resolver = getattr(shadow, "resolve_pending", None)
    if resolver is not None:
        resolver(engine, now=NOW + dt.timedelta(minutes=minutes_later))


def _only_row(engine):
    with get_session(engine) as s:
        (row,) = s.query(ShadowMakerOrder).all()
        s.expunge(row)
        return row


# --------------------------------------------------------------------------

def test_1_a_fill_in_tape_recorded_after_placement_is_found(engine, monkeypatch):
    _place(engine, monkeypatch)
    _covered(engine)
    _print(engine, NOW + dt.timedelta(seconds=10))     # arrives AFTER placement

    _resolve(engine)

    assert _only_row(engine).status == "filled"


def test_1b_until_the_window_is_judged_the_order_is_pending_not_unfilled(engine, monkeypatch):
    _place(engine, monkeypatch)
    assert _only_row(engine).status == "pending"


def test_2_the_order_rests_at_bid_plus_one_never_at_the_taker_price(engine, monkeypatch):
    _place(engine, monkeypatch, yes_bid=44, yes_ask=46)
    row = _only_row(engine)
    assert row.start_price_cents == 45
    assert row.start_price_cents < row.taker_price_cents


def test_2b_a_one_cent_spread_joins_the_bid(engine, monkeypatch):
    _place(engine, monkeypatch, yes_bid=45, yes_ask=46)
    assert _only_row(engine).start_price_cents == 45


def test_2c_the_walk_never_reaches_the_taker_price(engine, monkeypatch):
    _place(engine, monkeypatch, yes_bid=40, yes_ask=46)
    row = _only_row(engine)
    assert row.cap_cents < row.taker_price_cents


def test_3_category_and_series_reach_the_row_from_the_scorer(engine):
    """The scorer's opportunity carried no category; the row said 'unknown'."""
    from src.ev.scorer import score_all_markets  # noqa: F401  (import check)
    import inspect

    import src.ev.scorer as scorer

    source = inspect.getsource(scorer.score_all_markets)
    assert '"category": category' in source
    assert '"traded_net_ev"' in source


def test_3b_the_report_is_per_series(engine, monkeypatch):
    _place(engine, monkeypatch)
    text = shadow.format_report(shadow.report_by_series(engine))
    assert "KXHIGHNY" in text and "unknown" not in text


def test_4a_a_gap_flagged_by_another_market_on_the_subscription_is_unproven(engine, monkeypatch):
    _place(engine, monkeypatch)
    _covered(engine)
    _print(engine, NOW + dt.timedelta(seconds=10))
    with get_session(engine) as s:
        s.add(OrderbookGap(market_ticker=OTHER, sid=2, expected_seq=5, received_seq=9,
                           missing=4, detected_at=NOW + dt.timedelta(seconds=20)))
        s.commit()

    _resolve(engine)

    assert _only_row(engine).status == "unproven"


def test_4b_a_gap_detected_just_after_the_window_is_unproven(engine, monkeypatch):
    """The missing messages were inside the window; the message that revealed
    the gap arrived after it."""
    _place(engine, monkeypatch)
    _covered(engine)
    from src.trading_config import MAKER_MAX_STEPS, MAKER_REST_SECONDS
    rest = MAKER_REST_SECONDS * (MAKER_MAX_STEPS + 1)
    with get_session(engine) as s:
        s.add(OrderbookGap(market_ticker=OTHER, sid=1, expected_seq=5, received_seq=9,
                           missing=4, detected_at=NOW + dt.timedelta(seconds=rest + 5)))
        s.commit()

    _resolve(engine)

    assert _only_row(engine).status == "unproven"


def test_4c_a_gap_after_the_connection_moved_on_does_not_count(engine, monkeypatch):
    _place(engine, monkeypatch)
    _covered(engine)                                     # a row at +5 min
    with get_session(engine) as s:
        s.add(OrderbookGap(market_ticker=OTHER, sid=1, expected_seq=5, received_seq=9,
                           missing=4, detected_at=NOW + dt.timedelta(minutes=8)))
        s.commit()

    _resolve(engine)

    assert _only_row(engine).status == "unfilled"


def test_5_the_maker_leg_pays_the_maker_fee_which_is_zero_on_weather(engine, monkeypatch):
    # Tape already present so even the old synchronous judge would fill.
    _covered(engine)
    _print(engine, NOW + dt.timedelta(seconds=10))
    _place(engine, monkeypatch)
    _resolve(engine)

    row = _only_row(engine)
    assert row.status == "filled"
    assert row.maker_fee == 0
    assert row.taker_fee > 0


def test_6_no_recorder_coverage_is_unproven_after_two_hours_not_unfilled(engine, monkeypatch):
    _place(engine, monkeypatch)                          # no snapshot, no rows

    _resolve(engine, minutes_later=30)
    assert _only_row(engine).status == "pending"         # rows may still be in flight
    _resolve(engine, minutes_later=125)
    assert _only_row(engine).status == "unproven"


def test_production_resolves_pending_orders_every_cycle():
    """L27: the resolver must be reachable from the cycle, not only from tests."""
    import inspect

    import src.run_trading as rt

    assert "resolve_pending(" in inspect.getsource(rt._run_pipeline_locked)


class TestWipeVoidRows:
    """Ruling: the 28 pre-rebuild rows are void and are wiped. They are
    exactly the rows the old code wrote, which never stored a plan."""

    def _rows(self, engine):
        from src.models.shadow import ShadowMakerOrder

        with get_session(engine) as s:
            for planned in (None, None, 1):
                s.add(ShadowMakerOrder(
                    market_id=MARKET, side="yes", intended_quantity=Decimal("3"),
                    start_price_cents=46, cap_cents=60, status="unfilled",
                    taker_price_cents=46, planned_steps=planned,
                ))
            s.commit()

    def test_dry_run_counts_and_changes_nothing(self, engine, monkeypatch, capsys):
        from tests.test_maintenance_entrypoint import run_module

        self._rows(engine)
        code, out = run_module(["--wipe-void-shadow"], engine, monkeypatch, capsys)
        assert code == 0 and "would delete 2" in out
        with get_session(engine) as s:
            assert s.query(ShadowMakerOrder).count() == 3

    def test_the_token_deletes_only_pre_rebuild_rows(self, engine, monkeypatch, capsys):
        from tests.test_maintenance_entrypoint import run_module

        self._rows(engine)
        code, _ = run_module(["--wipe-void-shadow", "--confirm", "WIPE-VOID-SHADOW"],
                             engine, monkeypatch, capsys)
        assert code == 0
        with get_session(engine) as s:
            assert [r.planned_steps for r in s.query(ShadowMakerOrder).all()] == [1]

    @pytest.mark.parametrize("bad", ["WIPE", "SHRINK-TAPE", "PURGE-ORPHAN-MARKETS"])
    def test_any_other_token_changes_nothing(self, engine, monkeypatch, capsys, bad):
        from tests.test_maintenance_entrypoint import run_module

        self._rows(engine)
        code, _ = run_module(["--wipe-void-shadow", "--confirm", bad], engine, monkeypatch, capsys)
        assert code == 2
        with get_session(engine) as s:
            assert s.query(ShadowMakerOrder).count() == 3
