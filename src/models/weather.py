"""Persisted per-cell calibration, and the archive that will replace its source.

Two tables, both existing because the pipeline is a fresh process every five
minutes:

  weather_cell_fits   the fitted bias/width per (station, lead) plus the
                      held-out scores that decided whether it may price. A fit
                      recomputed per cycle would be refitted from whatever
                      history happened to be reachable at that moment.

  forecast_archive    every forecast we see, recorded daily whether or not it
                      is traded on. MOS comes from a third-party archive; this
                      is how we earn the right to stop depending on it, and it
                      cannot be backfilled later — a day not recorded is gone.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class WeatherCellFit(Base):
    """Fitted error model for one (station, lead), with its held-out verdict."""

    __tablename__ = "weather_cell_fits"
    __table_args__ = (
        UniqueConstraint("station", "lead_days", "predictor", name="uq_weather_cell"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    station: Mapped[str] = mapped_column(String(12), index=True)
    lead_days: Mapped[int] = mapped_column(Integer, index=True)
    predictor: Mapped[str] = mapped_column(String(20), default="mos")

    bias: Mapped[float] = mapped_column(Float)
    sigma: Mapped[float] = mapped_column(Float)

    # Everything below is measured on HELD-OUT pairs. Scores from the fit
    # window would say nothing about skill.
    n_fit_pairs: Mapped[int] = mapped_column(Integer, default=0)
    n_eval_pairs: Mapped[int] = mapped_column(Integer, default=0)
    brier: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    brier_skill: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    reliability_slope: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)

    # The promotion decision, stored rather than recomputed, so what the model
    # is allowed to do cannot drift from what was actually measured.
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reasons: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    fit_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    fit_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    eval_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    eval_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    fitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class GuardState(Base):
    """Last known pause state per cell, so transitions can be detected.

    Without persistence the pipeline cannot tell "paused" from "paused since
    yesterday", and a cell flapping in and out of pause every few cycles is
    exactly the signal worth seeing — it means the threshold is sitting on top
    of the live Brier rather than safely away from it.
    """

    __tablename__ = "guard_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    last_brier: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    transitions: Mapped[int] = mapped_column(Integer, default=0)
    changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class ForecastArchive(Base):
    """One forecast, recorded the day we saw it.

    Written every cycle regardless of whether anything trades. The migration
    off the third-party MOS archive depends entirely on having accumulated
    these, and unlike a fit they cannot be reconstructed after the fact.
    """

    __tablename__ = "forecast_archive"
    __table_args__ = (
        UniqueConstraint(
            "station", "target_date", "lead_days", "source",
            name="uq_forecast_archive",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    station: Mapped[str] = mapped_column(String(12), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    lead_days: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))     # "mos" | "gridpoint"
    max_temp_f: Mapped[float] = mapped_column(Float)
    runtime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
