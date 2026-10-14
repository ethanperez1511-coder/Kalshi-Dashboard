"""Paper-trading summary, read-only.

    python -m src.maintenance --paper-summary

Two scopes, side by side, so the difference can be explained:
  ALL           every paper trade, including legacy rows (is_legacy), such as
                the trades placed by the broken fee deploy.
  GATE-COUNTED  the trades the 50-trade live gate counts (legacy excluded).

The bankroll is equity at cost (portfolio/tracker.py): a fill leaves it
untouched and a settlement moves it by that trade's realized P&L, net of the
entry fee. So open positions are carried at what was paid for them. The
report reconciles the bankroll against START_BANKROLL plus every settled
paper trade's realized P&L, and prints any gap instead of hiding it.

Nothing here writes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Engine

from src.database import get_session
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.trade import Trade

# The same starting point the tracker's return figure uses.
START_BANKROLL = 100.0


# Series -> city. Explicit: station names (stations.py) are the contracts'
# location strings ("Central Park, New York", "Austin Bergstrom") and do not
# reduce to a city by rule.
CITIES = {
    "KXHIGHNY": "New York", "KXHIGHCHI": "Chicago", "KXHIGHMIA": "Miami",
    "KXHIGHDEN": "Denver", "KXHIGHAUS": "Austin", "KXHIGHLAX": "Los Angeles",
    "KXHIGHPHIL": "Philadelphia", "KXHIGHTATL": "Atlanta", "KXHIGHTBOS": "Boston",
    "KXHIGHTDAL": "Dallas", "KXHIGHTDC": "Washington DC", "KXHIGHTLV": "Las Vegas",
    "KXHIGHTMIN": "Minneapolis", "KXHIGHTNOLA": "New Orleans",
    "KXHIGHTOKC": "Oklahoma City", "KXHIGHTPHX": "Phoenix",
    "KXHIGHTSATX": "San Antonio", "KXHIGHTSEA": "Seattle", "KXHIGHTSFO": "San Francisco",
}


def _city(market_id: str) -> str:
    """The city for a weather ticker; any other ticker reports its series."""
    series = market_id.split("-", 1)[0]
    return CITIES.get(series, series)


@dataclass
class Scope:
    total: int = 0
    settled: int = 0
    open: int = 0
    other: int = 0                       # cancelled / pending: never filled
    # Filled rows whose market has no open position. Settlement closes only
    # the newest filled trade per market (tracker.close_position), so an
    # older row on the same market keeps "filled" after its position closed.
    # Counted apart: they are neither open nor carry a realized P&L.
    stranded: int = 0
    other_statuses: Dict[str, int] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    flat: int = 0
    realized: float = 0.0
    fees: float = 0.0
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    largest_win: Optional[Tuple[float, str]] = None
    largest_loss: Optional[Tuple[float, str]] = None
    by_side: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    by_city: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    pnls: List[float] = field(default_factory=list)
    # Average cost of the side bought, as a probability: the win rate a
    # trade at that price needs just to break even (before fees).
    breakeven_win_rate: Optional[float] = None

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None


@dataclass
class Summary:
    bankroll: float
    peak_bankroll: float
    reconciliation_gap: float
    open_positions: int
    open_cost: float
    open_market_value: float
    legacy: List[Tuple[int, str, str, int, int, str, Optional[float]]]
    scopes: Dict[str, Scope]


def _scope(trades: List[Trade], open_markets: set) -> Scope:
    s = Scope(total=len(trades))
    win_amounts, loss_amounts, prices = [], [], []
    for t in trades:
        if t.status == "closed":
            s.settled += 1
            pnl = float(t.realized_pnl or 0.0)
            s.pnls.append(pnl)
            s.realized += pnl
            s.fees += float(t.entry_fee or 0.0)
            prices.append(t.price / 100)
            if pnl > 0:
                s.wins += 1
                win_amounts.append(pnl)
                if s.largest_win is None or pnl > s.largest_win[0]:
                    s.largest_win = (pnl, t.market_id)
            elif pnl < 0:
                s.losses += 1
                loss_amounts.append(pnl)
                if s.largest_loss is None or pnl < s.largest_loss[0]:
                    s.largest_loss = (pnl, t.market_id)
            else:
                s.flat += 1
            n, total = s.by_side.get(t.side, (0, 0.0))
            s.by_side[t.side] = (n + 1, total + pnl)
            city = _city(t.market_id)
            n, total = s.by_city.get(city, (0, 0.0))
            s.by_city[city] = (n + 1, total + pnl)
        elif t.status == "filled":
            if t.market_id in open_markets:
                s.open += 1
            else:
                s.stranded += 1
        else:
            s.other += 1
            s.other_statuses[t.status] = s.other_statuses.get(t.status, 0) + 1
    s.avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else None
    s.avg_loss = sum(loss_amounts) / len(loss_amounts) if loss_amounts else None
    s.breakeven_win_rate = sum(prices) / len(prices) if prices else None
    return s


def summarize(engine: Engine) -> Summary:
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        trades = session.query(Trade).filter(Trade.is_paper.is_(True)).order_by(Trade.id).all()
        positions = session.query(Position).filter_by(status="open").all()

        open_markets = {p.market_id for p in positions}
        all_scope = _scope(trades, open_markets)
        gate_scope = _scope([t for t in trades if not t.is_legacy], open_markets)
        legacy = [
            (t.id, t.market_id, t.side, t.price, t.quantity, t.status, t.realized_pnl)
            for t in trades if t.is_legacy
        ]
        bankroll = float(settings.bankroll) if settings else 0.0
        peak = float(settings.peak_bankroll) if settings else 0.0
        return Summary(
            bankroll=bankroll,
            peak_bankroll=peak,
            reconciliation_gap=round(bankroll - (START_BANKROLL + all_scope.realized), 2),
            open_positions=len(positions),
            open_cost=sum(p.cost_basis for p in positions),
            open_market_value=sum(p.current_price * p.quantity / 100 for p in positions),
            legacy=legacy,
            scopes={"all": all_scope, "gate": gate_scope},
        )


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.2f}"


def _edge_lines(s: Scope) -> List[str]:
    n = len(s.pnls)
    if n < 2:
        return [f"  edge: {n} settled trade(s); too few for any statistic"]
    mean = sum(s.pnls) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in s.pnls) / (n - 1))
    se = sd / math.sqrt(n)
    lines = [
        f"  mean P&L per settled trade : {_money(mean)}  (std dev {sd:.2f}, std error {se:.2f})",
        f"  95% range for the true mean: {_money(mean - 1.96 * se)} to {_money(mean + 1.96 * se)}",
    ]
    if s.win_rate is not None and s.breakeven_win_rate is not None:
        decided = s.wins + s.losses
        wr_se = math.sqrt(s.win_rate * (1 - s.win_rate) / decided) if decided else 0.0
        lines.append(
            f"  win rate {s.win_rate:.1%} ± {1.96 * wr_se:.1%} (95%) vs ~{s.breakeven_win_rate:.1%} "
            f"needed to break even at the prices paid (before fees)"
        )
    verdict = (
        "the range includes zero: NOT enough evidence of an edge either way"
        if mean - 1.96 * se <= 0 <= mean + 1.96 * se else
        "the range excludes zero at 95%, but one test on a small sample; treat as tentative"
    )
    lines.append(f"  verdict: {verdict}")
    return lines


def _scope_lines(title: str, s: Scope, start: float) -> List[str]:
    ending = start + s.realized
    lines = [
        f"{title}",
        f"  trades: {s.total} total · {s.settled} settled · {s.open} open"
        + (f" · {s.other} never filled {s.other_statuses}" if s.other else "")
        + (f" · {s.stranded} filled with no open position (no P&L recorded)" if s.stranded else ""),
        f"  realized P&L: {_money(s.realized)} ({s.realized / start:+.2%} of ${start:.2f}); "
        f"bankroll on this basis ${ending:.2f}; entry fees paid {s.fees:.2f}",
        f"  wins {s.wins} · losses {s.losses}" + (f" · flat {s.flat}" if s.flat else "")
        + (f" · win rate {s.win_rate:.1%}" if s.win_rate is not None else ""),
        f"  average win {_money(s.avg_win)} · average loss {_money(s.avg_loss)}",
        "  largest win  " + (f"{_money(s.largest_win[0])} ({s.largest_win[1]})" if s.largest_win else "n/a"),
        "  largest loss " + (f"{_money(s.largest_loss[0])} ({s.largest_loss[1]})" if s.largest_loss else "n/a"),
        "  by side: " + " · ".join(
            f"{side.upper()} {n} trades {_money(p)}" for side, (n, p) in sorted(s.by_side.items())),
        "  by city:",
    ]
    for city, (n, p) in sorted(s.by_city.items(), key=lambda kv: kv[1][1]):
        lines.append(f"    {city:<16} {n:>3} settled  {_money(p)}")
    lines.extend(_edge_lines(s))
    return lines


def format_summary(r: Summary) -> str:
    a = r.scopes["all"]
    lines = [
        "PAPER TRADING SUMMARY (read-only)",
        "",
        f"Starting bankroll ${START_BANKROLL:.2f} · current ${r.bankroll:.2f} · "
        f"total P&L {_money(r.bankroll - START_BANKROLL)} "
        f"({(r.bankroll - START_BANKROLL) / START_BANKROLL:+.2%}) · peak ${r.peak_bankroll:.2f}",
        f"Reconciliation: start + realized P&L of every settled paper trade = "
        f"${START_BANKROLL + a.realized:.2f}; gap to the stored bankroll {r.reconciliation_gap:+.2f}"
        + ("" if abs(r.reconciliation_gap) < 0.01 else
           "  <- NOT ZERO: the bankroll has history these rows do not explain"),
        "",
        f"Open positions: {r.open_positions}, carried in the bankroll at cost "
        f"(${r.open_cost:.2f} paid). At the last recorded market prices they are worth "
        f"${r.open_market_value:.2f} ({_money(r.open_market_value - r.open_cost)} unrealized), "
        f"which is NOT in the bankroll until they settle.",
        "",
    ]
    lines += _scope_lines("ALL paper trades (legacy included)", a, START_BANKROLL)
    lines += [""]
    lines += _scope_lines("GATE-COUNTED paper trades (legacy excluded)", r.scopes["gate"], START_BANKROLL)
    lines += ["", f"Legacy rows ({len(r.legacy)}): the difference between the two scopes"]
    for tid, market, side, price, qty, status, pnl in r.legacy:
        lines.append(f"  #{tid} {market} {side.upper()} {qty} @ {price}c  {status}  P&L {_money(pnl)}")
    return "\n".join(lines)
