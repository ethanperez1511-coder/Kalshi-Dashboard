# Phase 3 design — maker-first execution

**Status:** design only, not built. Awaiting approval.

---

## 0. What execution may and may not do

Execution decides HOW we trade, never WHETHER. No gate, model, threshold or
risk limit changes in this phase. The maker path receives an already-approved
`TradeDecision` and changes only the price it rests at and the fills it
records. If maker execution is disabled, every decision the system makes is
byte-identical.

Concretely, the maker layer sits strictly downstream of `RiskManager.evaluate`
and never calls back into the scorer, the filter, or the limits checker except
to *re-check* limits on a partial fill (§4), which can only reduce exposure.

---

## 1. The fill rule

### The rule

For a resting **YES bid at price P**, fill only when a print satisfies **all**:

| Condition | Why |
|---|---|
| `taker_outcome_side == "no"` | a NO taker consumes resting YES bids — verified on 2,299 trades across 6 markets, 100% agreement with the matching `orderbook_delta` |
| `yes_price_dollars < P` | strict trade-**through**, not touch |
| `is_block_trade == false` | block trades match off-book and never touch the public ladder |
| reconstructed book confirms our order was resting at P at that `ts_ms` | we cannot fill an order that had already been cancelled or filled |
| no sequence gap spans the resting interval | across a gap the book is unreconstructable, so resting cannot be confirmed |

Mirror image for a resting NO bid: `taker_outcome_side == "yes"` and
`no_price_dollars < P`.

### Why this needs no queue assumption

Kalshi matches on strict price-time priority and consumes bids best-first. A
taker that reached a price *worse* than P must have exhausted **the entire
level at P**, including our order, wherever we sat in the queue. Queue position
is not observable in public data (confirmed absent — every book level is
exactly `[price, size]`), so any rule that needed it would be guesswork. This
one does not.

### What it understates, quantified

It discards **every partial fill that occurs at our level without trading
through it**. Measured on the probe sample: only **121 of 1,200 taker events
(10%) touched two or more price levels**; 67% were a single print. So the rule
recognises roughly the top decile of taker activity as fills.

**Direction of the error is one-way: we under-report maker fills.** A real
maker order would fill more often than the simulation says. Consequences:

- Simulated maker fill *rate* is a **lower bound**. Treat it as such and never
  quote it as an expected fill rate.
- Simulated maker **PnL per filled trade** is not biased by this — the trades
  it does recognise are ones we would certainly have got. The bias is in the
  count, not the price.
- Adverse selection is **over-represented**: a trade-through means the market
  moved decisively against our resting side. So the fills we do count are
  disproportionately the ones we would least like to have. Reported maker edge
  is therefore pessimistic on two axes at once, which is the correct direction
  but means a *negative* result is not conclusive evidence maker is bad.

### The rule that is explicitly rejected

Inferring fills from shrinking book levels. Over 71 seconds on one market,
**269 negative deltas, of which 250 (92.9%) had no trade at the same
timestamp**; cancel volume 25,839 contracts against 297 traded. Diffing polled
or streamed book state would **overstate maker capture by ~87x**. Almost all
liquidity disappearance on Kalshi is quote-pulling. This approach is not a
fallback and will not be used.

---

## 2. Fractional contracts

`count_fp` is fractional: **1,541 of 2,299 observed trades had non-integer
counts** (min 0.01, median 10, max 10,042.69). Any integer assumption is wrong
money math.

Current code assumes integers in at least: `TradeDecision.quantity`,
`Position.quantity`, `Trade.quantity` (all `Integer` columns),
`kelly_size`'s `int(dollars / contract_cost)`, `cost_basis`, `unrealized_pnl`,
and the fee formula.

**Design:** carry quantity as `Decimal` end to end, persisted as `Numeric`.

- Not float. Money and share counts that are compared, summed and checked
  against limits should not carry binary-rounding error; a 40%-filled order
  reconciling to 0.3999999 against an exposure cap is a bug that appears once a
  quarter and is untraceable.
- Migration is additive per existing discipline: new `Numeric` columns
  alongside the `Integer` ones, backfilled from them (every historical value is
  a whole number, so the backfill is exact and lossless), then reads switch
  over. Old rows stay valid.
- Rounding happens exactly once, at order placement, to whatever tick Kalshi
  accepts (`price_level_structure: deci_cent`, `price_ranges.step: 0.0010` on
  the market we inspected — the step is per-market and must be read, not
  assumed).
- Every limit check, Kelly size, fee and PnL computation operates on `Decimal`.

**Tests:** a 0.01-contract fill, a 10,042.69-contract fill, a fill whose size
is not representable in binary floating point, and an exposure check that must
not drift after 100 fractional partials.

---

## 3. Unfilled-order handling

Every parameter is config with the current value as default, because these are
exactly the knobs the Phase 4 experiment framework should sweep.

| Parameter | Default | Meaning |
|---|---|---|
| `MAKER_REST_SECONDS` | 30 | how long to rest before stepping (existing `REQUOTE_SECONDS`) |
| `MAKER_STEP_CENTS` | 1 | walk-up increment |
| `MAKER_MAX_STEPS` | 3 | steps before giving up |
| `MAKER_MAX_PRICE_RULE` | `model_minus_edge` | ceiling: `p_model` minus the required edge, so walking up can never consume the edge that justified the trade |
| `MAKER_TIMEOUT_SECONDS` | 300 | total lifetime, then cancel |
| `MAKER_ENABLED` | **false** | off until validated (§5) |

The max-price rule is the important one: the walk-up stops at the price where
the trade would no longer have passed the EV filter. Execution cannot trade
away the edge the model found — that would be execution changing *whether* we
trade, which §0 forbids.

---

## 4. Partial fills as first-class

A partial fill is the normal case, not an exception.

- **Position sizing:** the position is created at the filled quantity, not the
  intended one. Already true on the live path; must hold in simulation.
- **Exposure limits:** re-checked against the *remaining* order before each
  walk-up step. A partial fill has already consumed exposure, and stepping up
  adds more. This is the one place execution calls back into the risk layer,
  and it can only shrink the order.
- **PnL:** cost basis is the filled quantity at the filled price. Fees are
  charged per fill, not per order — Kalshi charges per execution, so a
  three-step walk-up that fills in three pieces pays three fees, and simulating
  one fee per order would understate cost.
- **Cancellation:** cancelling the remainder never touches the filled part.

**Tests:** a 40%-filled order — position quantity, cost basis, exposure
contribution, fee total and settled PnL all correct; a partial fill followed by
a step-up that would breach the exposure cap is refused; three partial fills at
three prices produce one position with a weighted-average entry and three fees.

---

## 5. What can be validated, and when

This is the honest core of the design.

| Claim | Validated by | Available |
|---|---|---|
| Trade-through detection is correct | Replay recorded trade tape against recorded book; assert every detected fill has a matching `orderbook_delta` consuming our level | **Now** — tape reaches back 60 days |
| Maker/taker side mapping | Already verified: 2,299 trades, 100%, 0 violations | **Now** |
| Book reconstruction is faithful | Apply recorded deltas to a recorded snapshot; compare against an independently fetched REST orderbook at the same moment | **~1 day** of recording |
| Simulated maker fill rate | Requires our order to have been resting during recorded intervals, which requires recorded book history covering the intervals we would have quoted | **N days** — see below |
| Maker vs taker spread capture | The whole justification for the phase. Needs enough filled maker simulations to be more than noise | **N days** |

### What N is, and why

The binding constraint is **filled maker simulations**, not calendar days.

With the trade-through rule recognising ~10% of taker events, and paper trades
currently accruing at roughly a handful per day across the qualifying set, a
defensible sample of maker fills needs on the order of **30 days of continuous
recording** to produce enough filled simulations to separate maker capture from
noise at the spreads involved (1–3 cents on contracts priced in whole cents).

**I am not confident in that number and will not pretend otherwise.** It rests
on an assumed trade frequency in the specific markets we quote, which is
measurable directly from the recorder after a few days and is not measurable
before. Therefore:

> **After 7 days of recording, compute the observed rate of qualifying
> trade-throughs in the markets we actually score, and derive N from it.
> Report that number before building the maker execution path.**

If the honest answer at that point is that our markets trade too thinly for a
trade-through rule to ever produce a usable sample, **that is the deliverable**:
maker capture cannot be measured at our resolution, and the phase should stop
rather than ship an unvalidated fill model. The weather series are thin — the
one probed produced no deltas in 15 seconds — so this is a live possibility.

### Book reconstruction has no history at all

`orderbook_delta` is live-only. There is **no historical book feed**. Every
claim in the table above that depends on book state can only be validated over
intervals we recorded ourselves, starting today. The 60-day tape does not help:
knowing a trade printed tells us nothing about whether our hypothetical order
was still resting.

---

## 6. Instrumenting the gap

For every simulated maker order, record on the trade row:

- the maker limit price and every walk-up step taken
- the taker price that was available at decision time (`yes_ask` for a YES buy)
- **realised capture** = taker price − maker fill price, per contract, in cents
- the fee difference (maker vs taker under Kalshi's formula)
- whether it filled, and if not, at which step it expired

Aggregate reported per model and per category, since spread capture on a
1-cent-wide weather market and a 4-cent-wide parlay are different businesses.

**This is the phase's justification and must be measured, not assumed.** If
realised capture net of fees does not exceed the taker path, maker execution
does not ship regardless of how well the simulator works.

---

## 7. Rollout

1. Recorder runs (**shipped today**).
2. Day 7: report observed trade-through frequency; derive N; decide whether to
   continue.
3. Build simulator; validate book reconstruction against REST snapshots.
4. Run maker simulation in **shadow** — computing what a maker order would have
   done while taker paper fills continue unchanged, so trade count keeps
   accruing toward the 50-trade gate and nothing about the current record
   changes.
5. After N days, report capture. `MAKER_ENABLED` flips only if capture net of
   fees beats taker on the same signals.

**Paper maker mode stays OFF throughout.** Taker paper fills continue exactly
as now. The 50-trade gate keeps accruing on the current, validated execution
path, and shadow simulation writes to its own table without touching `trades`.

---

## 8. What I could not verify, and what would change the design

- **Queue position for a real resting order.** Requires placing one. Hard
  constraints forbid live execution, so the rule is designed to need no queue
  assumption at all.
- **Whether `seq` stays gap-free under load.** Zero gaps in 521 messages on one
  market; not tested on a high-fanout subscription or across a reconnect. The
  recorder logs gaps rather than assuming their absence.
- **Block trade behaviour.** Never observed one — every trade in the sample was
  `is_block_trade: false`. The exclusion is inferred from "matches off-book"
  and should be confirmed if one ever appears.
- **Sustained rate ceiling.** `/account/limits` gives 400 capacity / 200 refill
  at cost 10 per request, but the observed burst behaviour does not reconcile
  cleanly with it. The recorder runs over websocket and costs zero REST tokens,
  so this does not bind — but pacing must mirror `/account/limits`
  client-side rather than be tuned from observed burst numbers.
