"""Telegram alert notifications for the trading pipeline."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        resp = httpx.post(
            _BASE.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


class Alerter:
    def __init__(self, token: str = "", chat_id: str = ""):
        self._token = token or os.environ.get("TELEGRAM_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.warning(
                "Telegram alerts DISABLED — TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set. "
                "No trade/settlement/error/heartbeat notifications will be sent."
            )

    def send(self, text: str) -> None:
        if self._enabled:
            _send(self._token, self._chat_id, text)

    def trade(self, market_id: str, side: str, qty: int, price: int, dollars: float, is_paper: bool) -> None:
        tag = "📝 PAPER" if is_paper else "💰 LIVE"
        self.send(
            f"{tag} TRADE\n"
            f"<b>{market_id}</b>\n"
            f"{side.upper()} ×{qty} @ {price}¢ (${dollars:.2f})"
        )

    def settled(self, market_id: str, pnl: float, bankroll: float) -> None:
        icon = "✅" if pnl >= 0 else "❌"
        self.send(
            f"{icon} SETTLED: <b>{market_id}</b>\n"
            f"PnL: ${pnl:+.2f} | Bankroll: ${bankroll:.2f}"
        )

    def milestone(self, count: int, total: int) -> None:
        self.send(f"🎯 Paper trades: <b>{count}/{total}</b>")

    def cycle_summary(self, cycle: int, trades: int, bankroll: float, paper_count: int) -> None:
        self.send(
            f"🔄 Cycle {cycle}\n"
            f"Trades: {trades} | Bankroll: ${bankroll:.2f} | Paper: {paper_count}/50"
        )

    def heartbeat(
        self,
        bankroll: float,
        paper_count: int,
        total: int,
        coverage: dict = None,
        per_model: dict = None,
        weather: str = "",
    ) -> None:
        """Daily liveness ping, carrying the two coverage facts worth waking to.

        `coverage` answers "of the threshold contracts that exist, how many can
        we actually price?" A parser that quietly stops reading them looks
        identical to a quiet market otherwise.

        `per_model` answers "what is the paper record actually made of?" A 50/50
        gate reached entirely by one model says "validated" about a system that
        is only validated in one corner.
        """
        lines = [f"✅ Alive — bankroll ${bankroll:.2f} | paper {paper_count}/{total}"]

        if coverage:
            priceable = coverage.get("priceable", 0)
            unreadable = coverage.get("unreadable", 0)
            if priceable or unreadable:
                line = f"📊 Threshold contracts: {priceable}/{priceable + unreadable} priceable"
                if unreadable:
                    line += f" — ⚠️ {unreadable} unreadable"
                lines.append(line)

        if per_model is not None:
            breakdown = ", ".join(
                f"{name} {count}"
                for name, count in sorted(per_model.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"🧩 Paper trades by model: {breakdown or 'none attributed'}")

        if weather:
            lines.append(weather)
        lines.append("(daily heartbeat)")
        self.send("\n".join(lines))

    def error(self, msg: str) -> None:
        self.send(f"🚨 PIPELINE ERROR\n<code>{msg[:300]}</code>")

    def live_gate_reached(self) -> None:
        self.send(
            "🚀 <b>50 paper trades complete!</b>\n"
            "Review performance and flip mode=live when ready."
        )
