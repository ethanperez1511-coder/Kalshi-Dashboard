# Live size ramp: design (for approval, not built)

**Rule (2026-10-09):** 1 contract for each of the first 20 live trades, then
step-ups against a stated evidence bar. The ramp can only LOWER a trade's
size. It never raises a cap.

## Where it sits

```
p_model → EV gate → RiskManager (quarter-Kelly, 3% single, 25% exposure,
          daily loss, 20% drawdown, cluster cap) → quantity_K
        → RAMP: quantity = min(quantity_K, stage_cap)       ← the only change
        → kill switches → TradeEngine
```

- It applies **only when `mode == "live"`.** Paper sizing is untouched, so
  the paper record stays comparable with its own history.
- It runs **after** every existing limit, as a `min()`. By construction it
  cannot exceed what risk approved. A test pins that for every stage and
  every input, `ramped_quantity <= risk_quantity`, and that no risk constant
  moved.
- A stage cap is a **contract count**, not a dollar figure, so a price move
  cannot enlarge it.

## Stages

| Stage | Live trades | Max contracts per trade | Leaves when |
|---|---|---|---|
| 0 | 1–20 | **1** | evidence bar met, and a human promotes |
| 1 | 21–40 | 3 | evidence bar met, and a human promotes |
| 2 | 41–80 | 10 | evidence bar met, and a human promotes |
| 3 | 81+ | no ramp cap (Kelly and the hard limits only) | — |

The trade counts are minimums. A stage never ends early, however good the
numbers look.

## The evidence bar (all must hold over the stage's own live trades)

1. **Execution.** Mean |fill − evaluated price| ≤ 1¢, worst ≤ 2¢, and no
   `fill_slippage` halt during the stage.
2. **Fees.** Every live fill's charged fee equals the fee schedule to the
   cent (fee_schedule.py against Kalshi's fill record).
3. **Ledger parity.** Every settled live trade's PnL equals Kalshi's own
   settlement to the cent, and positions in our database equal Kalshi's
   `/portfolio/positions` at every cycle, with zero unexplained differences.
4. **No halts.** Not one kill switch tripped during the stage, for any
   reason.
5. **Calibration, as a sanity bound, not a verdict.** The realized win rate
   is within 2 binomial standard errors of the mean `p_model`. At n = 20 that
   band is wide (about ±22 points at p = 0.5) and is meant to be: twenty
   trades cannot prove edge. They can expose a broken model or a broken
   execution path, which is what this stage is for.

**Bars 1–4 test plumbing, and they are the point.** Bar 5 is deliberately
weak; edge is proven by the paper record and by volume over time, not by a
20-trade stage.

## Promotion and demotion

- **Promotion is human, never automatic.**
  - The digest prints the eligibility for each bar: "stage 0: 20/20 trades,
    bars 1-5 ✅".
  - Moving up takes a typed
    `python -m src.maintenance --promote-ramp --confirm PROMOTE-RAMP`, which
    refuses unless every bar holds.
  - Code can say "eligible"; only a person says "go".
- **Demotion is automatic and immediate.**
  - Any kill-switch trip drops the ramp to **stage 0**, so the next live
    trade after a cleared halt is again 1 contract.
  - Climbing back requires a new stage's worth of clean evidence.
- **State:** `ramp_stage` and `ramp_stage_started_trade` in
  `TradingSettings`. Each change is one committed UPDATE with who and why,
  recorded in a `ramp_events` row.

## Tests the build must carry (failing first)
- Stage 0: risk approves 7 contracts, the ramp sends 1.
- No stage, at any input, sends more than risk approved (a property test over
  stages × quantities).
- Paper mode: the ramp is inert, and quantity equals risk's quantity.
- Promotion with any bar unmet is refused; the wrong token is refused.
- A kill-switch trip demotes to stage 0, and the next live trade is 1
  contract.
- Every hard limit (Kelly 0.25, 3%, 25%, 20%) is unchanged; the existing
  pinned tests stay green.

## Out of scope
Turning live trading on. The live gate (`mode == "live"` AND ≥ 50 paper
trades, flipped by a human) is unchanged, and this ramp only governs what
happens after that flip.
