from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from sqlalchemy import Engine
from src.database import get_session
from src.portfolio.equity import total_equity
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.trading_config import MAX_CLUSTER_EXPOSURE

logger = logging.getLogger(__name__)


@dataclass
class LimitsResult:
    approved: bool
    violations: List[str] = field(default_factory=list)


# MVE tickers name their event in the middle segment, so that segment alone is
# the correlated group. Every other ticker's middle segment is a date.
_MVE_PREFIX = "KXMVE"


def _extract_cluster_key(market_id: str) -> str:
    """Extract a cluster key from a Kalshi market ID.

    For MVE tickers like KXMVESPORTSMULTIGAMEEXTENDED-S2026XXXX-YYYY the middle
    segment is the event/collection and groups the correlated legs, so it is
    the key on its own.

    For everything else the middle segment is a DATE, and using it alone was
    wrong in both directions. Measured:

        KXHIGHNY-26AUG13-T92   -> '26AUG13'
        KXHIGHAUS-26AUG13-T99  -> '26AUG13'
        KXHIGHMIA-26AUG13-T88  -> '26AUG13'

    Every temperature contract on one date shared a cluster across all seven
    cities under a single 10%-of-bankroll cap — New York and Miami weather are
    not correlated — while the thing that genuinely is correlated, the ladder of
    strikes on one city-day, was grouped only incidentally through the shared
    date. Series plus date is the correlated unit: one station, one day, one
    outcome that every strike on the ladder reads off.

    The previous docstring claimed a fall back to the full market_id; the code
    never did that.
    """
    parts = market_id.split("-")
    if len(parts) < 2:
        return market_id
    if parts[0].startswith(_MVE_PREFIX):
        return parts[1]          # the event/collection
    return f"{parts[0]}-{parts[1]}"   # series + date = one correlated outcome


class LimitsChecker:
    def __init__(self, engine: Engine):
        self._engine = engine

    def _get_settings(self) -> dict:
        with get_session(self._engine) as session:
            s = session.query(TradingSettings).first()
            if not s:
                return {}
            return {
                "bankroll": s.bankroll,
                "peak_bankroll": s.peak_bankroll,
                "max_single_trade_pct": s.max_single_trade_pct,
                "max_total_exposure_pct": s.max_total_exposure_pct,
                "max_correlated_exposure_pct": s.max_correlated_exposure_pct,
                "daily_loss_limit_pct": s.daily_loss_limit_pct,
                "drawdown_circuit_breaker_pct": s.drawdown_circuit_breaker_pct,
            }

    def _get_total_exposure(self) -> float:
        with get_session(self._engine) as session:
            positions = session.query(Position).filter_by(status="open").all()
            total = sum(p.cost_basis for p in positions)
        return total

    def _get_daily_pnl(self) -> float:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with get_session(self._engine) as session:
            trades = (
                session.query(Trade)
                .filter(Trade.status == "closed")
                .filter(Trade.created_at >= today_start)
                .all()
            )
            total_pnl = sum(t.realized_pnl or 0 for t in trades)
        return total_pnl

    def check(self, trade_dollars: float, market_id: str, market_category: str) -> LimitsResult:
        settings = self._get_settings()
        if not settings:
            return LimitsResult(approved=False, violations=["No trading settings found"])

        violations = []
        # Every limit divides by total equity — cash plus the mark-to-market
        # value of open positions — and never by the raw ledger field. That is
        # what makes paper and live size identically: the two paths keep the
        # same ledger, and this is the one number derived from it.
        bankroll = total_equity(self._engine)

        # 1. Max single trade
        max_single = bankroll * settings["max_single_trade_pct"]
        if trade_dollars > max_single:
            violations.append(
                f"Single trade ${trade_dollars:.2f} exceeds max ${max_single:.2f} "
                f"({settings['max_single_trade_pct']:.0%} of bankroll)"
            )

        # 2. Total exposure
        current_exposure = self._get_total_exposure()
        max_exposure = bankroll * settings["max_total_exposure_pct"]
        if current_exposure + trade_dollars > max_exposure:
            violations.append(
                f"Total exposure ${current_exposure + trade_dollars:.2f} exceeds max ${max_exposure:.2f} "
                f"({settings['max_total_exposure_pct']:.0%} of bankroll)"
            )

        # 3. Daily loss limit
        daily_pnl = self._get_daily_pnl()
        max_daily_loss = bankroll * settings["daily_loss_limit_pct"]
        if daily_pnl < 0 and abs(daily_pnl) >= max_daily_loss:
            violations.append(
                f"Daily loss ${abs(daily_pnl):.2f} exceeds limit ${max_daily_loss:.2f} "
                f"({settings['daily_loss_limit_pct']:.0%} of bankroll) — paused"
            )

        # 4. Drawdown circuit breaker.
        # Peak is a realized high-water mark; current is marked to market, so an
        # underwater book trips the breaker before it is booked as a loss.
        peak = settings["peak_bankroll"]
        if peak > 0:
            drawdown = (peak - bankroll) / peak
            if drawdown >= settings["drawdown_circuit_breaker_pct"]:
                violations.append(
                    f"Drawdown {drawdown:.1%} exceeds circuit breaker "
                    f"{settings['drawdown_circuit_breaker_pct']:.0%} — system stopped"
                )

        # 5. Correlation-aware cluster exposure
        cluster_violation = self._check_cluster_exposure(
            trade_dollars, market_id, bankroll,
        )
        if cluster_violation:
            violations.append(cluster_violation)

        return LimitsResult(
            approved=len(violations) == 0,
            violations=violations,
        )

    def _check_cluster_exposure(
        self, trade_dollars: float, market_id: str, bankroll: float,
    ) -> Optional[str]:
        """Cap exposure per correlated cluster.

        Clusters are defined by:
        - game_id: positions sharing the same game event (extracted from market_id)
        - (league, date): positions in the same league on the same day
        """
        max_cluster = bankroll * MAX_CLUSTER_EXPOSURE

        with get_session(self._engine) as session:
            positions = session.query(Position).filter_by(status="open").all()
            # Read values inside session to avoid DetachedInstanceError
            pos_data = [(p.market_id, p.cost_basis) for p in positions]

        new_cluster = _extract_cluster_key(market_id)

        cluster_exposure: Dict[str, float] = {}
        for mid, cost in pos_data:
            key = _extract_cluster_key(mid)
            cluster_exposure[key] = cluster_exposure.get(key, 0) + cost

        # Check if adding this trade exceeds cluster cap
        current = cluster_exposure.get(new_cluster, 0)
        if current + trade_dollars > max_cluster:
            return (
                f"Cluster exposure ${current + trade_dollars:.2f} exceeds "
                f"max ${max_cluster:.2f} ({MAX_CLUSTER_EXPOSURE:.0%} of bankroll) "
                f"for cluster {new_cluster}"
            )
        return None
