# Storage emergency: the file reaches 95% of Neon's cap

**What 100% means.** Neon refuses any write that needs new space, and that
includes deletes and retention's own cleanup. Reads keep working. Trading
cycles fail at their first write. September's 20-day outage was this. The aim
is to never let the FILE extend to the cap: space freed INSIDE the file is
what new writes use first.

Never run VACUUM FULL or execute shrink-tape here. Both need headroom that no
longer exists.

## Steps, in order

1. **Read the state** (1 min). Check the digest's `💾 DB` line or the last
   retention run summary: file, live, reusable, and the TABLES list. Check
   **Neon console → your project → Monitoring/Usage → Storage** for Neon's own
   figure. If **reusable ≥ 100 MB**, the file is not about to grow: watch,
   don't act. Act on the steps below when reusable is small, or the file is
   rising.

2. **Pause the biggest writer** (1 min). In GitHub: **Actions →
   market-session → ⋯ → Disable workflow.** This stops the recorder (most of
   the bytes) and the session cycles. Expect `session-watchdog` to alert "no
   session"; that is intended. **Leave `paper-trade-cycle` (the backstop)
   running**: its writes are small, and trading and settlement continue.

3. **Free space inside the file** (5 min). **Actions → maintenance →
   action `cut-tape`, days `7`, confirm blank** (dry run: read the row count),
   then the same with confirm **`CUT-TAPE`**. This deletes tape older than 7
   days (the floor) and VACUUMs. The file does not shrink, but about 46 MB per
   deleted day becomes reusable, and new writes go there.

4. **Run retention** (5 min). **Actions → retention → Run workflow**, with
   dry_run `false`. Payload nulling runs at any file level, in 20k-row
   batches with VACUUM after each, and stops once the file has grown ~2 MB
   in that run (ruling 2026-10-16; the old stop at 95% blocked it for good
   once crossed, because the file never shrinks). Its summary prints the
   new figures and the largest tables. With only a few MB of headroom, use
   **maintenance → `compact-tape`** instead (dry run, then `COMPACT-TAPE`):
   2,000-row batches, stops at 1 MB of growth.

5. **If the file is at or above 97% and still rising, take a pressure valve.**
   Two options; the first is preferred:
   - **Neon console → Billing → upgrade to Launch.** The 1 GB cap goes away
     at once. Storage is about $0.35/GB-month, so the overage costs cents. It
     needs a card. It is reversible later (downgrade after the move to
     Oracle).
   - **Last resort, irreversible:** **Neon console → SQL Editor:**
     `TRUNCATE orderbook_delta_raw;`. This returns the whole tape file at
     once (TRUNCATE swaps in an empty file; it does not need the space a copy
     needs). All recorded tape is lost: shadow evidence restarts from zero.
     Trading, positions and settlement are untouched. Expected to work at the
     cap; unverified there.

6. **Recover.** When **reusable ≥ 150 MB**, or after the upgrade:
   **Actions → market-session → Enable workflow.** Confirm the next digest
   shows the recorder and the session cycles back.

## What NOT to do
- VACUUM FULL or shrink-tape execute: both need free space.
- Deleting rows from `trades`, `positions`, `markets` or `settings`: that is
  the trading record, not the problem.
- Touching live mode: unrelated, and stays OFF.
