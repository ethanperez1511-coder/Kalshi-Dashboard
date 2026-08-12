"""Refuse to run a cycle on an uninitialised bankroll.

Kelly sizes every position as a fraction of equity, so a bankroll of zero sizes
everything to zero: every limit passes, every trade is approved, every order is
for nothing, and the cycle reports success having done nothing at all. It is the
worst failure shape in the system — indistinguishable from a quiet market, and
it would sit there accruing zero paper trades while the digest stayed green.

Where $100 comes from: `TradingSettings.bankroll` defaults to 100.0 in Python
and the row is created by `TradingSettings.get_or_create` on the first pipeline
run. A reported $0.00 before that first successful cycle means NO ROW EXISTS
yet, which is different from a zero balance and must be reported differently.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import Engine

from src.database import get_session
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)

# Below this, Kelly rounds every position to zero contracts.
MIN_WORKABLE_BANKROLL = 1.0


class BankrollNotInitialised(RuntimeError):
    """The bankroll cannot support any position. Names itself."""


def check_bankroll(engine: Engine) -> tuple:
    """(ok, message). Does not create the row — that is get_or_create's job."""
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        if settings is None:
            return True, (
                "no settings row yet — the first cycle will create it with the "
                "$100.00 paper default"
            )
        bankroll = settings.bankroll

    if bankroll is None or bankroll < MIN_WORKABLE_BANKROLL:
        return False, (
            f"bankroll is ${bankroll or 0:.2f}, below the ${MIN_WORKABLE_BANKROLL:.2f} "
            f"minimum. Kelly sizes every position as a fraction of equity, so "
            f"this sizes everything to zero: the cycle would approve trades, "
            f"place nothing, and report success. Set trading_settings.bankroll "
            f"to a real value."
        )
    return True, f"bankroll ${bankroll:.2f}"


def assert_bankroll_workable(engine: Engine) -> None:
    ok, message = check_bankroll(engine)
    if not ok:
        raise BankrollNotInitialised(message)
    logger.info("Bankroll check: %s", message)
