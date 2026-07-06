from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

if TYPE_CHECKING:
    from sqlalchemy import Engine

HEARTBEAT_INTERVAL_HOURS = 24


class TradingSettings(Base):
    __tablename__ = "trading_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    mode: Mapped[str] = mapped_column(String(20), default="paper")
    max_single_trade_pct: Mapped[float] = mapped_column(Float, default=0.03)
    max_total_exposure_pct: Mapped[float] = mapped_column(Float, default=0.25)
    max_correlated_exposure_pct: Mapped[float] = mapped_column(Float, default=0.10)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float, default=0.05)
    drawdown_circuit_breaker_pct: Mapped[float] = mapped_column(Float, default=0.20)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25)
    paper_trades_before_live: Mapped[int] = mapped_column(Integer, default=50)
    peak_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    paper_trade_count: Mapped[int] = mapped_column(Integer, default=0)
    # Liveness signal — last time a daily "still alive" heartbeat was sent.
    # Nullable, non-risk field: never gates trading, only reassurance alerts.
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)

    @classmethod
    def get_or_create(cls, engine: Engine) -> TradingSettings:
        from src.database import get_session

        with get_session(engine) as session:
            existing = session.query(cls).first()
            if existing is None:
                existing = cls()
                session.add(existing)
                session.flush()

            # Extract all values to a plain copy to avoid DetachedInstanceError
            copy = cls(
                id=existing.id,
                bankroll=existing.bankroll,
                mode=existing.mode,
                max_single_trade_pct=existing.max_single_trade_pct,
                max_total_exposure_pct=existing.max_total_exposure_pct,
                max_correlated_exposure_pct=existing.max_correlated_exposure_pct,
                daily_loss_limit_pct=existing.daily_loss_limit_pct,
                drawdown_circuit_breaker_pct=existing.drawdown_circuit_breaker_pct,
                kelly_fraction=existing.kelly_fraction,
                paper_trades_before_live=existing.paper_trades_before_live,
                peak_bankroll=existing.peak_bankroll,
                paper_trade_count=existing.paper_trade_count,
                last_heartbeat_at=existing.last_heartbeat_at,
            )
        return copy

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        # SQLite/Postgres may return naive datetimes — treat stored values as UTC.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @classmethod
    def heartbeat_due(cls, engine: Engine) -> bool:
        """True if no heartbeat sent yet, or the last one is >= 24h old."""
        from src.database import get_session

        with get_session(engine) as session:
            row = session.query(cls).first()
            last = row.last_heartbeat_at if row else None
        if last is None:
            return True
        age_h = (datetime.now(timezone.utc) - cls._as_utc(last)).total_seconds() / 3600.0
        return age_h >= HEARTBEAT_INTERVAL_HOURS

    @classmethod
    def record_heartbeat(cls, engine: Engine, at: Optional[datetime] = None) -> None:
        """Stamp the heartbeat time on the settings row."""
        from src.database import get_session

        stamp = at or datetime.now(timezone.utc)
        with get_session(engine) as session:
            row = session.query(cls).first()
            if row is not None:
                row.last_heartbeat_at = stamp
