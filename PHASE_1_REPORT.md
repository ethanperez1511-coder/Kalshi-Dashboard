# Phase 1 — Fix the known bleeders

**Date:** 2026-08-11
**Tests:** 325 passing (250 before this phase, 75 added). Zero failures.
**Mode:** paper, unchanged. Bankroll $101.35, 11/50 paper trades, all untouched.

---

## Safety statement

- No code path added or modified in this phase reads or writes `TradingSettings.mode`,
  `paper_trades_before_live`, or `can_trade_live()`. The live gate is exactly where it was:
  a human must set `mode='live'` in the DB **and** 50 clean paper trades must exist.
- Risk ceilings untouched — quarter-Kelly, 3%/trade, 25% exposure, 10% cluster, 5% daily
  loss, 20% drawdown breaker. `tests/test_host_migration.py` still asserts every one of them.
- All three changes either make PnL more conservative or reject **more** matches. None widens
  the set of tradeable markets, so over-exposure cannot worsen.
- Schema changes are additive only: two nullable columns, three new tables. Nothing dropped,
  no historical row rewritten.

---

## 1.1 — Fee-accurate settlement on both paths

### What was wrong

`src/portfolio/tracker.py:66`

```python
fee = kalshi_fee(pos.quantity, pos.entry_price) if is_paper else 0.0
```

Paper subtracted a simulated entry fee. Live subtracted nothing — the real fee is charged by
Kalshi at entry and never entered the DB — so every live `Trade.realized_pnl` overstated by up
to $0.02/contract.

That is not merely a reporting bug. `realized_pnl` feeds `src/portfolio/metrics.py` (win rate,
calibration error), the equity curve, and the Kelly shrinkage multiplier in
`src/risk/kelly.py:16` **which sizes real money**. Cash itself self-healed only because
`sync_live_bankroll` overwrites the bankroll from Kalshi every cycle, which hid it.

**A second bug surfaced while fixing it.** `_mark_trade_filled` debited the entry cost from the
bankroll at fill, and then settlement credited `realized_pnl` — which already has the entry cost
inside it. The live path was subtracting the entry cost twice. Again masked by the Kalshi sync.

### What changed

- `Trade.entry_fee` (dollars) and `Trade.entry_fee_source` recorded at fill time.
  Paper stores the simulated fee; live reads the real one from `get_fills(order_id=...)`
  (`KalshiFill.fee`, cents). If that lookup fails it falls back to the published formula and
  labels itself `"estimated"` — **never 0.0**, because a silent zero is the original bug.
- Settlement now uses **one formula for both paths**. The `is_paper` branch is gone from the
  PnL calculation.
- The `is_paper` distinction moved to where it actually belongs — the *cash ledger*:
  paper bankroll is equity-at-cost and moves once, at settlement, by realized PnL; live
  bankroll is real cash, debited cost+fee at fill and credited the payout at settlement.
  Both net to `gross - fee` over the round trip. That is what killed the double-debit.
- `src/database.py:ensure_schema()` — additive migration (`ALTER TABLE ... ADD COLUMN`),
  called from `run_trading.py` and `main.py`. There is no Alembic here and `create_all` cannot
  add a column to the existing 134k-row database or to Neon.

### Evidence

- `tests/test_settlement_fees.py` — 14 tests. The headline one runs an identical paper trade and
  live trade with the same fee and asserts **byte-identical** `realized_pnl` (before: $5.00 vs
  $4.82 for the same economics). Also covers real-fee settlement, the fills-endpoint-down
  fallback, zero-vs-null fee, legacy rows, and the round-trip cash ledger.
- Migration rehearsed on a **copy** of the real 95 MB `kalshi.db`: 11 trades / 6 positions /
  134,950 markets in, same count out, legacy rows null, second run a no-op.
- It also caught a **pre-existing** gap: `trading_settings.last_heartbeat_at`, added in the
  July host migration, was missing from the local DB. Any pre-existing database was missing it.

### Stale test updated

`test_live_position_no_simulated_fee` asserted live PnL was the fee-free gross number. Its cash
reasoning was right (don't double-charge) but its PnL conclusion was wrong. Replaced by
`test_live_position_settles_net_of_recorded_fee`, with the rationale in the docstring;
double-charging is now prevented in the ledger instead, under `TestCashLedger`.

---

## 1.2 — Odds quota

### What was wrong — worse than "the free tier is small"

`src/modeling/odds_api.py:31` held the cache in a **module-level dict**. That works in a
long-lived process. The bot moved to a GitHub Actions cron (`*/5 * * * *`) in July, so
**every cycle is a fresh Python interpreter and the cache is always empty**. The 60-minute TTL
has never applied in production.

Burn: 288 runs/day × 3 sports = **~864 requests/day** against a ~500/**month** allowance. The
quota died inside a day, and `SportsOddsModel` then returned `None` silently for the rest of
the month.

### What changed

- **The cache is now in the database** (`odds_cache`), so it survives the process. This is the
  fix that matters: ~864 requests/day becomes ~17.
- **Per-sport TTL derived from the budget**: `720h × n_sports / cap`. Three sports at a 500 cap
  gives ~4.3h, which spends exactly the monthly allowance and no more. Overridable per
  deployment via `TRADING_ODDS_TTL_MINUTES_OVERRIDE`.
- **A quota ledger** (`odds_quota`) charged *before* each request, hard-stopping at the cap
  rather than waiting for the API to refuse. Over-counting on a crash is the safe direction.
- **`TradeFilter.prescreen()`** — the market-only gates (volume, spread, expiry), runnable
  before model dispatch so no request is spent on a market that cannot qualify.
- **`OddsSource` interface** with `TheOddsApiSource` and `EspnOddsSource`.
- **`GET /api/quota`** + `QuotaCard.tsx` on Overview: spend, burn rate, month-end projection,
  days-to-exhaustion, per-sport cache age.

### ESPN — probed live before building, per the ground rules

Verified against the real endpoints on 2026-08-11, not from memory:

| League | Moneyline | Books | Coverage |
|---|---|---|---|
| MLB | yes | **1** (DraftKings) | 15/15 same-day |
| NBA | yes | **1** | 4/4 on an in-season date |
| NHL | yes | **1** | 8/8 on an in-season date |

Usable, so it is built — but with three constraints found by probing, each covered by a test:

1. The undated endpoint returns **yesterday's** slate, so `?dates=` is always sent explicitly.
2. Once a game is FINAL the `odds` key is **dropped entirely**, so a missing key is a skip.
3. `site.api.espn.com` sits behind an Akamai bot manager that 403s browser-like and custom
   User-Agents while passing library defaults. I confirmed httpx's default UA returns 200. A
   test asserts no custom UA is ever set — a well-meaning "identify ourselves" change would
   silently break this.

**It is one book.** Cross-book de-vig is impossible, so it inherits DraftKings' own juice
asymmetry, and it carries no odds outside the day of the game. It is therefore a fallback only,
**default OFF**, and confidence-capped at 0.70 vs 0.85 for the multi-book path.

### Evidence

`tests/test_odds_quota.py` — 23 tests, including the exact production regression: a cold
process with a warm DB cache makes **zero** HTTP calls. Plus exhausted-quota blocking, per-sport
TTL isolation, projection math on an injected clock, and ESPN parsing/fallback.
`tests/test_api_quota.py` — 2 endpoint contract tests.

### Prescreen is provably decision-neutral

`test_prescreen_rejection_implies_evaluate_rejection` sweeps a grid of volume × spread × expiry
and asserts that anything prescreen rejects, `evaluate()` also rejects. Skipping the model is an
optimisation, never a decision.

---

## 1.3 — Polymarket matching

### What was wrong — this was live and it was the worst of the three

`src/modeling/models/polymarket.py:72` compared numeric **tokens** but not the comparator
attached to them. Measured on realistic phrasing:

```
"Will the US Consumer Price Index inflation rate be above 3% in December 2026?"
"Will the US Consumer Price Index inflation rate be below 3% in December 2026?"
  similarity 0.818  (gate is 0.70)
  numeric tokens identical
  -> MATCHED
```

The model would take a price meaning the exact opposite of the market being priced. Negation
("will not happen") is one token in eight and survived the gate. Bare token sets are symmetric,
so "Yankees beat Red Sox" scored identically against "Red Sox beat Yankees".

### What changed

- `src/modeling/entities.py` — titles reduced to the fields that carry meaning: threshold
  **direction** and magnitude, dates, negation, and the ordered named parties. Any field that
  disagrees is a hard reject regardless of word overlap.
- `market_match_map` table — every pair ends up `approved` / `blocked` / `pending`.
  Approved mappings skip matching entirely and are reused forever. Blocked pairs are never
  matched again. **Pending produces no estimate** (your fail-closed decision).
- `GET /api/matches/pending`, `POST /api/matches/{id}/approve|block`, and a `Review` page
  showing both titles side by side with the field that triggered the stop.

### A second real bug, found by running it against live data

Comparing old vs new over 2,055 real Kalshi event-category markets and 1,500 live Polymarket
markets, the **first cut of the entity check still passed these**:

```
KALSHI: Will Alexandru Rafila be the next Prime Minister of Romania?
POLY  : Will Alexandru Nazare  be the next Prime Minister of Romania?
```

Two different candidates for the same office, matched on a shared **first name** — because I
compared entity *phrases* and the intersection was non-empty. Accented names made it worse: the
original ASCII regex mangled "Cătălin". Fixed by requiring every capitalised token to appear on
both sides, with Unicode-aware extraction. Regression tests added for both.

### Evidence — old vs new on live data

| | Result |
|---|---|
| Old fuzzy matcher | 17 markets priced off Polymarket |
| New | **13 priced, 4 stopped** |

The four stopped:

| Kalshi | Polymarket | Verdict |
|---|---|---|
| Alexandru **Rafila** … PM of Romania | Alexandru **Nazare** … PM of Romania | different person — real save |
| Cătălin **Drulă** … PM of Romania | Cătălin **Predoiu** … PM of Romania | different person — real save |
| Gadi **Eisenkot** … PM of Israel | Gadi **Eizenkot** … PM of Israel | same person, transliteration — your call |
| Max Martin attend Taylor Swift **and Travis Kelce's** wedding | … Taylor Swift's wedding | probably same — your call |

Two were fabricated data heading for the sizing engine. The other two are judgment calls that
now wait for you instead of trading blind. None are lost: one click each in Review, remembered
forever. The 13 correct matches are unaffected.

`tests/test_polymarket_entities.py` — 32 tests. `tests/test_api_matches.py` — 4.

---

## Flag status

| Flag | Default | Why |
|---|---|---|
| fee accounting | **ON** (no flag) | correction to wrong math; a flag would ship known-wrong PnL |
| `TRADING_POLYMARKET_REQUIRE_ENTITY_MATCH` | **ON** | correction; can only reject more, never accept more |
| `TRADING_ENABLE_ESPN_ODDS` | **OFF** | new capability; single-book data, verify before trusting |
| `TRADING_PRESCREEN_BEFORE_MODELS` | **OFF** | new behaviour; decision-neutral but stops writing Opportunity rows for prescreened-out markets |
| `TRADING_ODDS_MONTHLY_QUOTA` | 500 | set to your real plan limit |
| `TRADING_ESPN_MAX_CONFIDENCE` | 0.70 | vs 0.85 for multi-book |

---

## What I did NOT build, and why

- **Sport-demand filtering** (only fetch sports some prescreened Kalshi market references).
  The derived TTL already spends exactly the cap by construction, so this was not needed for
  survival. Worth adding when the sport list grows.
- **Per-sport live/pre-game TTL split** (shorter TTL near tip-off). The single derived TTL is
  what the budget supports; a shorter live TTL has to be paid for out of the same cap.
- **ESPN summary/core endpoints.** The scoreboard is enough for pre-game, which is all the
  model may use — it refuses commenced games. The core endpoint would matter for backfilling
  closing lines, which is Phase 4 CLV work.

## Things I want to flag rather than silently fix

**The live bankroll convention differs from paper, and it predates this phase.**
`sync_live_bankroll` sets `bankroll` to Kalshi's **cash** balance, which excludes the cost basis
of open positions. Paper's bankroll is equity-at-cost. The risk limits compare
`sum(cost_basis)` against `bankroll × 25%`, so in live mode the denominator shrinks as positions
open while the numerator grows — the exposure limit binds meaningfully **earlier** in live than
in paper.

That is the fail-safe direction, so nothing is unsafe. But it means the 50-trade paper
evaluation does not transfer cleanly to live sizing, which is the whole point of the paper
window. The fix is a one-line choice (sync `cash + cost_basis`, or use Kalshi's
`portfolio_value`) but it changes what every risk limit is measured against, so I am not making
that call unilaterally. Recommend addressing before the live switch, not before Phase 2.

---

## What you need to do

Nothing is required for Phase 1 to work — no new API keys, no new services.

1. **Nothing for the migration.** It runs automatically on the next cron tick against Neon.
   Your local `kalshi.db` was migrated during verification: 11 trades / 6 positions / 134,950
   markets before and after, `PRAGMA integrity_check` ok, `mode` still `paper`.
2. **Optional — set `TRADING_ODDS_MONTHLY_QUOTA`** as a GitHub Secret to your real plan limit
   if it is not 500. The derived TTL keys off it.
3. **Optional — turn on ESPN** with `TRADING_ENABLE_ESPN_ODDS=true` once you are comfortable
   with single-book data as a fallback.
4. **Work the review queue** at `/review` when Polymarket pairs appear. Two are waiting for a
   ruling as soon as the pipeline next scores those markets (Eisenkot/Eizenkot, and the Taylor
   Swift wedding pair).
