"""Run the full paper trading pipeline: score → risk → execute.

Supports continuous mode: python -m src.run_trading --loop --interval 300
"""
from __future__ import annotations
import argparse
import logging
import signal
import time

from src.alerts import Alerter
from src.config import Settings
from src.database import get_engine, verify_or_migrate
from src.demo.seed import seed_demo_data
from src.ingestion.live_ingest import ingest_live_markets
from src.ev.calculator import EVResult
from src.ev.scorer import score_all_markets
from src.kalshi.client import KalshiClient
from src.modeling.match_seed import apply_seed_decisions
from src.models.settings import TradingSettings
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.trading.engine import TradeEngine, sync_live_bankroll
from src.trading.settler import Settler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received, finishing current cycle...")
    _shutdown = True


def run_pipeline(alerter: Alerter | None = None, cycle: int = 0):
    alerter = alerter or Alerter()
    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    # Refuses to trade against a schema it does not recognise rather than
    # migrating on the way past. The Actions workflow runs `python -m
    # src.migrate` as its own explicit step before this.
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="the trading pipeline")

    # Seed demo data if offline, otherwise fetch live markets
    if settings.is_offline_mode:
        logger.info("Offline mode — using demo markets")
        seed_demo_data(engine)
    else:
        logger.info("Online mode — fetching live markets from Kalshi")
        ingest_live_markets(engine, settings)

    # Human match verdicts live in the repo so they reach Neon, which is only
    # writable from this runner. Idempotent; never overrides a dashboard decision.
    apply_seed_decisions(engine)

    # Ensure trading settings exist (bankroll=$100, paper mode)
    ts = TradingSettings.get_or_create(engine)
    logger.info(f"Bankroll: ${ts.bankroll:.2f} | Mode: {ts.mode} | Paper trades: {ts.paper_trade_count}")

    # Step 0: Settle any open positions whose markets have finalized
    kalshi_client = None
    if not settings.is_offline_mode:
        kalshi_client = KalshiClient.from_settings(settings)

    settler = Settler(engine, kalshi_client=kalshi_client)
    settled = settler.settle_all()
    if settled:
        logger.info(f"=== Settled {len(settled)} positions ===")
        for s in settled:
            logger.info(f"  {s['market_id']}: PnL ${s['realized_pnl']:.2f}")
        # Refresh bankroll after settlement
        ts = TradingSettings.get_or_create(engine)
        logger.info(f"Bankroll after settlement: ${ts.bankroll:.2f}")
        for s in settled:
            alerter.settled(s["market_id"], s["realized_pnl"], ts.bankroll)

    # Live mode only: bankroll mirrors the real Kalshi cash balance.
    # In paper mode the bankroll is virtual and must never be overwritten.
    if ts.mode == "live" and kalshi_client is not None:
        sync_live_bankroll(engine, kalshi_client)
        ts = TradingSettings.get_or_create(engine)

    # Step 1: Score all markets
    logger.info("=== Scoring markets ===")
    results = score_all_markets(engine)

    # Alert once if the sports odds feed has gone dark (quota/key) — otherwise
    # SportsOddsModel silently produces nothing and only Polymarket carries.
    from src.modeling import odds_api
    if odds_api.QUOTA_DEAD and not getattr(run_pipeline, "_quota_alerted", False):
        alerter.send("⚠️ Odds API quota dead — sports model dormant, Polymarket still active.")
        run_pipeline._quota_alerted = True
    qualifying = [r for r in results if r["status"] == "qualifying"]
    watching = [r for r in results if r["status"] == "watching"]
    rejected = [r for r in results if r["status"] == "rejected"]
    logger.info(f"Scored {len(results)} markets: {len(qualifying)} qualifying, {len(watching)} watching, {len(rejected)} rejected")

    # Daily liveness heartbeat — fires even on idle (zero-trade) cycles, before the
    # early return below, so a healthy-but-quiet system is distinguishable from a dead one.
    if TradingSettings.heartbeat_due(engine):
        alerter.heartbeat(ts.bankroll, ts.paper_trade_count, ts.paper_trades_before_live)
        TradingSettings.record_heartbeat(engine)

    for r in results:
        symbol = "✓" if r["status"] == "qualifying" else ("~" if r["status"] == "watching" else "✗")
        logger.info(
            f"  {symbol} {r['market_id']}: "
            f"edge={r['edge']:+.1%} ev={r['net_ev']:.4f} "
            f"p_model={r['p_model']:.0%} vs market={r['implied_prob']:.0%} "
            f"side={r['recommended_side']} [{r['status']}]"
        )

    if not qualifying:
        logger.info("No qualifying opportunities — nothing to trade.")
        return

    # Step 2+3: Risk evaluate and execute each qualifying opportunity
    rm = RiskManager(engine)

    te = TradeEngine(engine, kalshi_client=kalshi_client)
    trades_placed = 0

    logger.info("=== Evaluating and executing trades ===")
    for opp in qualifying:
        ev = EVResult(
            p_model=opp["p_model"],
            implied_prob=opp["implied_prob"],
            edge=opp["edge"],
            no_edge=-opp["edge"],
            raw_ev=opp["net_ev"],
            net_ev=opp["net_ev"],
            no_ev=-opp["net_ev"],
            recommended_side=opp["recommended_side"],
            fee_rate=0.01,
        )

        decision = rm.evaluate(
            ev_result=ev,
            confidence=opp["confidence"],
            market_id=opp["market_id"],
            market_category="General",
        )

        if not decision.approved:
            logger.info(f"  ✗ {opp['market_id']}: REJECTED — {decision.rejection_reasons}")
            continue

        result = te.execute(
            decision=decision,
            market_id=opp["market_id"],
            p_model=opp["p_model"],
            implied_prob=opp["implied_prob"],
            edge=opp["edge"],
            net_ev=opp["net_ev"],
            confidence=opp["confidence"],
            reasoning=opp.get("reasoning", "auto-scored"),
            yes_bid=opp.get("yes_bid", 0),
            yes_ask=opp.get("yes_ask", 0),
        )

        if result:
            trades_placed += 1
            tag = "PAPER" if result.get("is_paper", True) else f"LIVE [{result.get('status', '?')}]"
            logger.info(
                f"  ✓ {tag} TRADE: {result['market_id']} "
                f"{result['side'].upper()} ×{result['quantity']} @ {result['price']}¢ "
                f"(${result['dollars']:.2f})"
            )
            alerter.trade(
                result["market_id"], result["side"], result["quantity"],
                result["price"], result["dollars"], result.get("is_paper", True),
            )

    # Summary
    logger.info("=== Portfolio Summary ===")
    tracker = PortfolioTracker(engine)
    summary = tracker.get_summary()
    positions = tracker.get_open_positions()

    logger.info(f"Trades placed this run: {trades_placed}")
    logger.info(f"Bankroll: ${summary['bankroll']:.2f}")

    # Milestone alerts
    paper_count = ts.paper_trade_count
    if paper_count in (10, 25, 40, 50) or paper_count % 10 == 0:
        alerter.milestone(paper_count, ts.paper_trades_before_live)
    if paper_count >= ts.paper_trades_before_live:
        alerter.live_gate_reached()
    logger.info(f"Open positions: {summary['open_position_count']}")
    logger.info(f"Total exposure: ${summary['total_exposure']:.2f}")
    logger.info(f"Unrealized PnL: ${summary['unrealized_pnl']:.2f}")

    for p in positions:
        logger.info(
            f"  {p['market_id']}: {p['side'].upper()} "
            f"×{p['quantity']} @ {p['entry_price']}¢ → {p['current_price']}¢ "
            f"PnL ${p['unrealized_pnl']:.2f}"
        )


def run_loop(interval: int = 300):
    """Run pipeline continuously with `interval` seconds between cycles."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    alerter = Alerter()
    cycle = 0
    while not _shutdown:
        cycle += 1
        logger.info(f"=== Cycle {cycle} ===")
        try:
            run_pipeline(alerter=alerter, cycle=cycle)
        except Exception as exc:
            logger.exception("Pipeline error — will retry next cycle")
            alerter.error(str(exc))

        if _shutdown:
            break

        logger.info(f"Sleeping {interval}s until next cycle (Ctrl+C to stop)...")
        # Sleep in small increments so shutdown is responsive
        for _ in range(interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Trading loop stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi paper trading pipeline")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles (default: 300)")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.interval)
    else:
        run_pipeline()
