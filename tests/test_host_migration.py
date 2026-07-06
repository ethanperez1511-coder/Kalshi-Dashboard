"""Tests for the GitHub Actions + Neon Postgres host migration (2026-07-03).

Covers the host-independent code changes:
  1. base64-encoded PEM env var decodes (robust cloud secret transport).
  2. Alerter logs a WARNING when disabled (missing creds) so silence is visible in CI logs.
  3. Daily heartbeat: fires when never sent / >24h stale, stays quiet otherwise.
  4. Risk-limit guard: migration must not loosen any hardcoded ceiling/floor.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.alerts import Alerter
from src.database import Base, get_engine
from src.kalshi.auth import load_private_key_from_string
from src.models.settings import TradingSettings


def _pem_str() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


# --- 1. base64 PEM transport ------------------------------------------------

def test_load_key_from_raw_pem():
    assert isinstance(load_private_key_from_string(_pem_str()), rsa.RSAPrivateKey)


def test_load_key_from_literal_backslash_n_pem():
    # Railway-style: real newlines replaced with literal \n.
    mangled = _pem_str().replace("\n", "\\n")
    assert isinstance(load_private_key_from_string(mangled), rsa.RSAPrivateKey)


def test_load_key_from_base64_pem():
    # New robust path: base64 the whole PEM to dodge newline corruption entirely.
    b64 = base64.b64encode(_pem_str().encode()).decode()
    assert isinstance(load_private_key_from_string(b64), rsa.RSAPrivateKey)


# --- 2. Alerter loud when disabled ------------------------------------------

def test_alerter_warns_when_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        Alerter(token="", chat_id="")
    assert any("disabled" in r.message.lower() for r in caplog.records)


def test_alerter_no_warning_when_enabled(caplog):
    with caplog.at_level(logging.WARNING):
        Alerter(token="t", chat_id="c")
    assert not any("disabled" in r.message.lower() for r in caplog.records)


# --- 3. Daily heartbeat -----------------------------------------------------

def _engine():
    eng = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def test_heartbeat_due_when_never_sent():
    eng = _engine()
    TradingSettings.get_or_create(eng)
    assert TradingSettings.heartbeat_due(eng) is True


def test_heartbeat_not_due_right_after_recording():
    eng = _engine()
    TradingSettings.get_or_create(eng)
    TradingSettings.record_heartbeat(eng)
    assert TradingSettings.heartbeat_due(eng) is False


def test_heartbeat_due_again_after_24h():
    eng = _engine()
    TradingSettings.get_or_create(eng)
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    TradingSettings.record_heartbeat(eng, at=stale)
    assert TradingSettings.heartbeat_due(eng) is True


# --- 4. Risk-limit guard (CLAUDE.md invariant) ------------------------------

def test_risk_limits_unchanged_by_migration():
    eng = _engine()
    ts = TradingSettings.get_or_create(eng)
    assert ts.mode == "paper"                      # never live by default
    assert ts.max_single_trade_pct == 0.03         # 3% per trade
    assert ts.max_total_exposure_pct == 0.25       # 25% total exposure
    assert ts.drawdown_circuit_breaker_pct == 0.20 # 20% breaker
    assert ts.kelly_fraction == 0.25               # quarter-Kelly
    assert ts.paper_trades_before_live == 50       # live gate
    assert ts.paper_trade_count == 0               # fresh eval window
