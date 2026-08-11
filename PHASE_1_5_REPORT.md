# Phase 1.5 — Bankroll semantics, match rulings, migration safety

**Date:** 2026-08-11
**Tests:** 357 passing (325 after Phase 1, 32 added). Zero failures.
**Mode:** paper, unchanged. Bankroll $101.35, 11/50 paper trades.

> **Read this first.** Item 2 turned up something that invalidates a claim in
> the Phase 1 report. All 13 Polymarket pairs that phase called "correct
> matches" are structurally mismatched, and Polymarket coverage is now zero
> until reviewed. Detail in §2.

---

## 1. Bankroll semantics — unified on total equity

### The problem

Paper and live measured different things, and every risk limit divides by the
result:

| | Before |
|---|---|
| paper | `bankroll` untouched at fill, moved at settlement → equity-at-cost |
| live | entry cost debited at fill; `sync_live_bankroll` then overwrote the field with Kalshi's **cash** balance, which excludes open positions |

So in live the denominator shrank as positions opened while exposure grew, and
the 25% cap bound far earlier than in paper.

### The change

One definition, `src/portfolio/equity.py:total_equity()`:

```
total equity = cash + mark-to-market value of open positions
```

Both paths now keep the same equity-at-cost ledger — untouched at fill (buying
swaps cash for a position of equal cost), moved once at settlement by realized
PnL. Marking to market is then exactly adding unrealized PnL. Consequences:

- `_mark_trade_filled` no longer debits the bankroll. This also removed the
  Phase 1 payout-vs-PnL branch in `close_position` — the `is_paper` split is
  gone from the settlement ledger entirely, which is a net simplification.
- `sync_live_bankroll` stores `Kalshi cash + cost basis of open positions`,
  converting Kalshi's cash figure into the shared convention.
- `LimitsChecker` and `RiskManager` both call `total_equity()`. Nothing reads
  the raw `bankroll` field for sizing any more.
- The drawdown breaker now compares a realized high-water mark against a
  marked-to-market current value, so an underwater book trips it before the
  loss is booked. Strictly earlier, never later.

**No recomputation.** Past trades are untouched; the paper ledger's meaning did
not change (paper was already equity-at-cost), so the stored $101.35 is correct
as-is. Only the live path moved, and live has never executed.

### Evidence

`tests/test_equity_semantics.py` — 10 tests. The one you asked for builds the
identical economic state in a paper DB and a live DB and asserts:

- `total_equity(paper) == total_equity(live)`
- identical limit decisions **and identical violation strings** (`$5.00` vs a
  3% cap on $102.00 equity → both reject with "max $3.06")
- identical Kelly sizing and quantity

Plus ledger symmetry (neither path moves the bankroll at fill; both move by
realized PnL at settlement) and the sync conversion (\$95 cash + \$5 cost basis
→ \$100.00).

Three Phase 1 tests asserted the old live-debit behaviour and were superseded,
each with the rationale in the docstring.

---

## 2. Review-queue rulings — and what they exposed

### Max Martin / Taylor Swift → **BLOCKED**

Not a close call. The two contracts use directly contradictory standards, and
they have **already diverged in reality**: Kalshi settled YES on 2026-07-05;
Polymarket is still open and formally disputed at 68.4c.

| Axis | Kalshi | Polymarket |
|---|---|---|
| What counts | "reported present ... by any Source Agency, including social media posts by the person themselves"; "**virtual attendance counts**"; "brief appearances or partial attendance count" | "**Only physical attendance** ... virtual attendance or confirmation of an invitation will not count" |
| Evidence | 17 named press agencies | photo/video, or a statement from Swift, Kelce, the attendee, or their reps |
| No-wedding case | runs to 2028-12-31 | force-resolves **NO** if no wedding by 2026-12-31 |

If UMA settles NO for want of photo/video, Kalshi is YES and Polymarket is NO
on the same person at the same wedding. A cross-venue position loses both legs.

### Gadi Eisenkot / Eizenkot → **BLOCKED** (you asked for a horizon check first; it fails)

Same person — "Eizenkot" is a transliteration variant, as you said. But not the
same question. Verbatim from the live Gamma API:

> "This market will resolve to the next individual who is officially appointed
> and sworn in as Prime Minister of Israel **following the 2026 parliamentary
> election** ... If no such Prime Minister is sworn in **by December 31, 2027**,
> 11:59 PM ET, this market will resolve to 'Other'."

The Kalshi contract has no election scoping and closes 2045-01-01. So
P(Polymarket YES) ≤ P(Kalshi YES) strictly, and the gap always points the same
way: using Polymarket's 68c as `p_model` understates Kalshi and manufactures a
durable "NO is cheap" edge that cannot be arbitraged and would not surface
until settlement, years out. Per your instruction — approve only if it passes —
it is blocked, with that quote as the recorded reason.

### The horizon check, and the thing it found

Added `POLYMARKET_MAX_HORIZON_GAP_DAYS` (default 90, ON): a pair whose
contracts resolve over materially different windows goes to review instead of
producing a price.

I measured it against live data before trusting the default, and the result was
not what I expected:

```
entity-matched pairs: 13
  with both horizons known: 13
  gap days: min=6576.6  median=6576.6  max=6576.6
  tolerance   7d ->  0/13 priced, 13 queued
  tolerance  90d ->  0/13 priced, 13 queued
  tolerance 365d ->  0/13 priced, 13 queued
```

Every single pair, identical 18-year gap. That pattern looks like a placeholder
comparison, so I checked whether Polymarket's descriptions carry a real
deadline rather than an administrative `endDate` — they do, quoted above.

**So the gap is real, and the Phase 1 report was wrong.** I wrote that the 13
surviving matches were "correct" and "unaffected". They match on *identity*;
they do not match on *resolution window*. Every one is a Romania-PM or
Israel-PM contract with the same structural defect as Eisenkot, and every one
was feeding a downward-biased `p_model` into the EV filter.

**Net effect: Polymarket coverage is currently zero.** All 13 now land in the
review queue. That is the correct answer — those prices were biased — but it
means the Polymarket model contributes nothing until either a genuinely
horizon-aligned pair appears or you approve one deliberately. Given that
Phase 1 already gated the price-derived models off, the practical position is
that SportsOdds is the only model currently able to trade.

### Persisting decisions

Decisions live in the DB, and the DB that matters is Neon, writable only from
the cron runner. So `src/modeling/match_seed.py` holds the rulings in the repo
and applies them idempotently at the start of each cycle. A seeded verdict
fills an empty slot or upgrades a `pending` row and **never** overrides a
decision you make in the dashboard (`decided_by == "human"` wins).

---

## 3. Migrations only when invoked

- `MIGRATE_ON_BOOT` (default **false**). `create_app()` and `run_pipeline()`
  now call `verify_or_migrate()`, which inspects and **refuses**:

  ```
  DATABASE SCHEMA IS BEHIND THE MODELS — the dashboard API did not modify it.
    Missing: trades.entry_fee, trades.entry_fee_source
    This process will fail on any query touching the above.
    Fix: run  python -m src.migrate
  ```

- `python -m src.migrate` (and `--check`, which exits 1 when behind) is the only
  path that writes by default. Added as its own explicit step in the Actions
  workflow, before the trading run.
- `src/main.py` no longer builds the app at import time. A module-level
  `app = create_app()` combined with a raising schema check would make the
  module unimportable; a PEP 562 `__getattr__` builds it on first access, so
  `uvicorn src.main:app` still resolves (verified: 24 routes) while
  `import src.main` touches nothing.

### Two real bugs found while building this

1. **`inspect(engine)` caches reflection.** A schema check run after a
   migration in the same process reported the *old* shape — so a
   post-migration verification would have passed on a database that never
   changed. Fixed with `clear_cache()`.

2. **`Base.metadata` only knows about imported models.** `python -m src.migrate`
   imports Settings and database and nothing else, so it would have inspected a
   near-empty metadata, reported "schema up to date", migrated nothing, and
   left the pipeline to crash on the very column it was run to add. My two new
   Phase 1 models were also missing from `src/models/__init__.py`. Fixed with
   `load_all_models()`, plus a test that asserts every table is registered and
   that the CLI builds a virgin database completely.

The second one would have bitten on the next Neon deploy.

---

## Correction to the Phase 1 report

Also worth correcting: the Phase 1.5 research initially read the stale
`close_date` on the Swift row as an ingest bug. It is not — `market_sync.py:22`
correctly stores `km.close_time`. That row is simply two months stale (last
snapshot 2026-06-12) in the local file, and the 30-minute stale-snapshot guard
already prevents it from being scored. Production runs every 5 minutes on Neon.

---

## What you need to do

1. **Nothing for the migration** — the workflow now runs `python -m src.migrate`
   as its own step.
2. **Expect zero Polymarket trades.** 13 pairs are queued at `/review`. Each is
   a "next PM" contract with the horizon defect; my recommendation is to block
   them as a group rather than approve, but they are yours to rule on.
3. **Decide whether Phase 2 should widen coverage deliberately.** With
   Polymarket effectively offline and price-derived models gated, SportsOdds is
   the only model that can currently place a trade — which raises the stakes on
   the weather model landing well.
