from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

if TYPE_CHECKING:
    from sqlalchemy import Engine


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
            )
        return copy
