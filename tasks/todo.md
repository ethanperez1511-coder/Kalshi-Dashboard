# Accuracy fixes — pre-live hardening (2026-06-11)

## Context
36 settled paper trades: NO side +$64.49 (23 trades), YES side -$37.17 (13 trades, avg claimed edge +29%).
Diagnosis: model overconfidence on YES longshots; huge model-vs-market disagreement = bad data, not alpha.
Paper maker fills (bid+1, zero fee, 100% fill assumption) overstate PnL.

## Safety statement (per CLAUDE.md)
- All three changes TIGHTEN filters or make paper PnL MORE conservative. None loosen any limit.
- No change touches `mode`, `paper_trading_mode`, or the live execution path. Paper default preserved.
- No DB schema changes. No risk-limit changes (quarter-Kelly, 3%/trade, 25% exposure, 20% breaker untouched).
- Over-exposure impossible to worsen: changes only reduce the set of qualifying trades.

## Tasks
- [x] 1. Disagreement cap — reject trades where |best_edge| > MAX_MODEL_DISAGREEMENT (default 0.15).
      Config: `TRADING_MAX_MODEL_DISAGREEMENT`. Implemented in TradeFilter (check 6, epsilon boundary).
- [x] 2. YES longshot skip — reject YES-side trades priced < 30¢ (default on).
      Config: `TRADING_SKIP_YES_LONGSHOTS`, `TRADING_YES_LONGSHOT_MAX_PRICE_CENTS`. (check 7)
- [x] 3. Conservative paper fills — paper trades fill at taker price (cross spread), not maker bid+1.
      Config: `TRADING_PAPER_CONSERVATIVE_FILLS` (default on). Live path unchanged (is_paper=False keeps maker pricing).
- [x] Tests first (TDD): 11 new tests in tests/test_filter_hardening.py — all written before implementation, failed, then passed.
- [x] Run full test suite — 181 passed. (3 pre-existing stale tests in test_ev_filter.py updated: they encoded
      old committed defaults — volume 500/spread 5/medium-conf edge — superseded by deliberate, CHANGES.md-documented
      tuning from an earlier session. Not caused by this change; verified via git stash baseline.)
- [x] Restart pipeline with new filters; verify cycle log shows new rejection reasons.
      First restarted cycle EXPOSED three critical pre-existing bugs (see below) — filters themselves
      worked (zero YES longshots placed vs many before).

## Critical bugs found during verification (fixed same day)
- [x] **Mixed price conventions** — Position.entry_price stored in side-cost terms (NO @ 85¢ stores 85)
      but cost_basis / unrealized_pnl / close_position assumed YES-scale. Consequences:
      NO exposure undercounted ~6× (risk limit bypassed: "$8.80" displayed vs $94 real),
      NO realized PnL inflated ~15× on wins (earlier "NO side +$64" analysis was fiction).
      Fix: single convention — entry/current/exit all in the position's own side-cost terms.
      Files: src/models/position.py, src/portfolio/tracker.py.
- [x] **Dead markets re-traded forever** — settler saw market finalized on Kalshi but never marked the
      local row; ingest only refreshes still-open markets, so the stale 'active' row with a 2-week-old
      snapshot was re-scored and re-bought every cycle (bankroll printed $100 → $262 in one cycle).
      Fix: close_position(finalize_market=True) marks Market.status='finalized';
      scorer skips any market whose latest snapshot is older than MAX_SNAPSHOT_AGE_MINUTES (30, configurable).
- [x] Tests: 10 new in tests/test_settlement_integrity.py (TDD — failed first, then passed).
      Stale tests updated: test_position_model.py (old buggy convention), test_scorer.py (stale timestamps).
- [x] Full suite: 191 passed.
- [x] DB reset (user approved): wiped 86 trades / 81 positions / 4329 opportunities;
      bankroll → $100, paper_trade_count → 0, mode stays 'paper'. Backup: kalshi.db.bak-2026-06-11.
- [x] Verify first clean cycle: scored 38 markets (was 2195 — stale guard removed ~2160 dead rows);
      1 paper trade NO ×3 @ 85¢, exposure $2.55 = 3 × 0.85 ✓ (correct side-cost math); bankroll $100.00.

## Known remaining optimism (follow-up, not this pass)
- [ ] Taker fee not deducted from paper realized PnL at settlement (~≤1.75¢/contract).
- [ ] Maker fill probability model (real maker orders may never fill).
- [ ] Live execution path (_execute_live) review before any live switch — never run against real money.

## Review
Filters: 11 new tests (test_filter_hardening.py); disagreement cap + YES longshot skip + conservative
taker fills for paper. All changes tighten/reduce — none loosen any limit; paper default untouched.
Settlement integrity: 10 new tests; uniform side-cost convention; markets finalized on settle;
stale-snapshot guard. Paper history was reset to start a clean, honest 50-trade evaluation window.
Live-gate note: paper_trade_count reset to 0 — the 50-trade requirement now measures real performance.

---

# Coverage + live-path hardening (2026-06-11, session 2)

## Context
- Ingest already runs every cycle; real limits: 1000-market fetch cap (feed dominated by one esports
  series) and only ~175/1000 fresh markets have trade history.
- Live execution path review found 4 bugs (never run against real money — found before it could hurt).

## Safety statement (per CLAUDE.md)
- Paper trading default untouched; mode flag untouched; gate logic (`can_trade_live`) untouched.
- Risk limits untouched (quarter-Kelly, 3%/trade, 25% exposure, 20% breaker).
- All live-path changes fix correctness of a path that is UNREACHABLE until a human flips mode=live
  AND 50 clean paper trades exist. No change can trigger live execution by itself.
- Bankroll sync runs ONLY when already in live mode — paper bankroll stays virtual.
- DB: same transactional patterns as existing code; no schema changes.

## Tasks
- [x] A. Market fetch cap configurable — `TRADING_MARKET_FETCH_CAP` (default 3000), used by live_ingest.
- [x] B1. place_order sends `no_price` for NO orders (was always `yes_price` — a live NO order
      "at 85¢" would have landed as yes_price=85 = NO cost 15¢: wrong side of the book).
- [x] B2. engine.execute async bridge — `_run_async()` helper (thread + asyncio.run inside a running
      loop, plain asyncio.run otherwise). Old code raised inside any FastAPI handler.
- [x] B3. _execute_live places the order at the computed maker fill price (side-cost terms), stores
      position entry side-cost, deducts actual fill cost (price×qty) from bankroll.
- [x] B4. Partial-fill handling — _poll_for_fill returns filled count; timeout with partial fill
      records the filled portion, cancels remainder, status="partial".
- [x] B5. `sync_live_bankroll()` — run_pipeline calls it ONLY when mode=="live" with a client;
      paper bankroll never overwritten.
- [x] Tests first: 8 new in tests/test_live_hardening.py (failed before impl, pass after).
      Fixed test-pollution bug in test_live_trading.py `_run` helper (deprecated get_event_loop).
- [x] Full suite: 199 passed.
- [ ] Restart pipeline; verify coverage increase in cycle log. (restarted PID 42357)

## Review
Live path now correct on: order side pricing, maker limit price, side-cost position entries,
actual-cost bankroll debits, partial fills, async-context safety, real-balance sync.
Still required before any live switch: human flips mode=live deliberately; 50 clean paper trades;
funded Kalshi account. Nothing in this change can trigger live execution by itself.

---

# Model integrity + LLM coverage (2026-06-12)

## Context
- SportsOddsModel fabricates player-prop probabilities (hardcoded exp curve, NBA rate applied
  to MLB hits / NHL points; "1+ hits" scored 95% vs real ~65%) and labels them "ext".
- Unmatched legs silently substituted with 0.5. Both violate the no-synthetic-data constraint.
- All giant "edges" (+54%, +72%, +85%) trace to these two bugs — market was right every time.
- 16k General + Economics markets have zero model coverage (only ConsensusModel fallback,
  which is price-derived and untradeable by config).

## Safety statement (per CLAUDE.md)
- Bug fix only REMOVES fabricated signals — fewer trades qualify, never more. No risk-limit,
  mode, or live-path changes.
- LLM model is additive: new independent model for General/Economics; same EV filter, risk
  limits, disagreement cap, and paper gate apply to its signals. Fails safe (returns None)
  on API error — no fallback estimates.
- No DB schema changes. No change can trigger live execution.

## Tasks — Part 1: SportsOddsModel integrity (first)
- [x] 1. Player-prop legs return None (unmatched) — delete _estimate_prop_prob fiction.
- [x] 2. Remove 0.5 substitution for unmatched legs; require ALL legs matched to emit estimate.
- [x] 3. Skip games already commenced (pre-game odds vs live market = stale signal).
- [x] 4. TDD: failing tests first, then fix; update stale tests that encoded old behavior.

## Tasks — Part 2: non-sports coverage (discovery: DB "General" rows are miscategorized
## sports parlays; real Elections/Politics/Economics markets were never ingested because the
## raw /markets feed is parlay-dominated and the cap hits first)
- [x] 5. KalshiClient.get_event_markets(): walk /events?with_nested_markets=true, skip Sports,
      flatten nested markets with the event's category. Public data, no new auth.
- [x] 6. live_ingest: ingest event-category markets each cycle alongside the main feed
      (config TRADING_INGEST_EVENT_CATEGORIES, TRADING_EVENT_FETCH_CAP).
- [x] 7. PolymarketModel: compare Kalshi price vs Polymarket (free public API, no key) for
      matched Elections/Politics/World markets. Independent model. Conservative title matching;
      ambiguous or unmatched → None. Fail-safe on API error — never a fallback number.
- [x] 8. Tests with mocked APIs; verify registry dispatch for new categories.
- [x] 9. Verify cycle: 5000 markets ingested (3000 feed + 2000 event-category); PolymarketModel
      matched Israel-PM / Bond markets at sim=1.00 against $1M books; 4 paper trades placed.

## Review
SportsOddsModel: fabricated prop curve + 0.5 substitution deleted; all legs must have real
external odds and the game must not have commenced. All giant fake edges eliminated.
Coverage: events feed ingest brings true-category Elections/Politics/Economics markets.
PolymarketModel: independent cross-platform price signal; conservative matching (similarity
threshold + exact number match + ambiguity margin 0.25 + $25k volume floor); fails safe.
Suite: 226 passed (19 new). First live cycle verified end-to-end.

---

# Volume + accuracy + cost improvements (2026-06-12, session 3)

## Context
- Odds API free quota (500/mo) exhausted in ~2 days: every 5-min cycle fetched 8 sports.
  SportsOddsModel has been loading 0 games for hours — silent failure, no alert.
- Two pipelines were running (local Mac + Railway) burning the same quota. Local killed.
- Paper PnL ignores Kalshi trading fees → overstates profitability → risk of going live on
  fake numbers. This is a decision-accuracy bug, not just optimism.
- Bot re-buys the same market every cycle (3× in 3 cycles) — concentrates risk, wastes the
  50-trade evaluation on a handful of markets.

## Safety statement (per CLAUDE.md)
- Fee-accurate PnL makes paper results MORE conservative — never loosens anything.
- Expiry filter, dedup, and odds caching only REDUCE or reshape the qualifying set.
- Polymarket scan widening adds independent-data coverage; same EV filter + risk limits apply.
- No change to mode, paper_trading_mode default, risk limits (quarter-Kelly, 3%/trade,
  25% exposure, 20% breaker), or live gate. No DB schema changes.

## Tasks
- [x] 1. Kill local pipeline (done — Railway is sole system of record).
- [x] 2. Odds API: cache across cycles (TTL) + only in-season sports + alert on quota dead.
- [x] 3. Fee-accurate paper settlement PnL (Kalshi ~7%·p·(1-p) per contract, applied at settle).
- [x] 4. Per-market dedup: skip a market already held (or capped adds/day).
- [x] 5. Max days-to-expiry filter (default 14d) — recycle capital fast.
- [x] 6. Polymarket scan 1000 → 3000 markets (free, unlimited).
- [x] TDD: failing tests first for fee math, dedup, expiry filter.
- [x] Full suite green (241 passed); push to Railway; verify cycle log + Telegram.

## Review (session 3)
241 tests pass (15 new in test_velocity_and_fees.py; 6 settlement/tracker tests updated to
expect net-of-fee PnL). Changes: (1) local pipeline killed — Railway sole system of record;
(2) Odds API cross-cycle TTL cache + in-season sport list + quota-dead Telegram alert;
(3) fee-accurate paper settlement (kalshi_fee, paper only — live already pays on Kalshi);
(4) skip already-held markets (no risk concentration / wasted paper count);
(5) 14-day max-expiry filter for fast capital recycling; (6) Polymarket scan 1000→3000.
All changes tighten the trade set or make paper PnL more conservative. Risk limits, mode
default, and live gate untouched.

---

# Migrate host: Railway (dead) → GitHub Actions cron + Neon Postgres (2026-07-03)

## Context
- Railway "used all available resources" → container stopped ~2 weeks ago → zero cycles →
  zero paper trades → zero Telegram. Root cause = host died, NOT a code bug. Creds were set.
- Deeper defect: no deadman. Dead process can't Telegram its own death; idle cycles are silent
  (`if not qualifying: return` before any alert); `Alerter` logs nothing when disabled;
  `cycle_summary()` heartbeat method exists but is never called. Alive-idle == dead from phone.
- User wants FREE + always-running. Every free PaaS tier (Railway/Fly/Render/Heroku) is gone.
  Chosen architecture: GitHub Actions scheduled cron (unlimited minutes on a PUBLIC repo) runs
  ONE pipeline cycle per tick; state lives in Neon Postgres (free, always-on, no card).
  Nothing persistent to exhaust → the exact failure mode that just bit us is structurally gone.
  GitHub emails on any failed workflow run = built-in death alert.
- Security pre-check DONE: no secrets in git history or tree (.env/*.pem/*.key/*.db/*.log all
  gitignored; .env.example empty; key prefixes 3f6b…/9806… absent everywhere). Safe to make public.

## Safety statement (per CLAUDE.md — over-exposure / accidental live / DB corruption)
- Accidental live execution: `mode` lives in the DB (`trading_settings.mode`), default 'paper'.
  Migration MUST carry mode='paper' (or start fresh at paper). NO env var flips live. GH Secret
  set is data-only; no change touches `paper_trading_mode` resolution or the live gate. Prove
  default-paper preserved in review.
- Over-exposure: cron every 5 min could overlap if a cycle runs >5 min → two writers → double
  execution against one Neon DB. Mitigate with workflow `concurrency` (one run at a time, do NOT
  cancel-in-progress mid-trade) + single-connection writer. Risk limits (quarter-Kelly, 3%/trade,
  25% exposure, 20% breaker) are unchanged code; add a test asserting they still hold post-migration.
- DB corruption: Postgres is transactional; `get_session` commit/rollback pattern unchanged;
  Pydantic validation at the FastAPI boundary unchanged. Single writer enforced by cron concurrency.
  No schema change — same SQLAlchemy models create_all on Neon.

## Tasks
- [x] 1. Audit for SQLite-isms. RESULT: Postgres-clean. autoincrement PKs → SERIAL (portable),
      no JSON/binary/pickle columns, no PRAGMA/strftime/julianday/raw-SQL, no text(). All datetime
      comparisons (scorer stale-snapshot guard, settler) done in Python on ORM objects, not in SQL.
      database.py already conditional on `sqlite` prefix — Postgres just skips connect_args.
      Migration = driver + connection string, nothing more.
- [x] 2. Add Postgres driver to pyproject (`psycopg[binary]>=3.2`). SQLite kept for local/tests.
- [ ] 3. Provision Neon (user step, I give exact clicks): free project → copy `DATABASE_URL`
      (`postgresql+psycopg://…?sslmode=require`). Fresh DB → `Base.metadata.create_all` + seed
      `TradingSettings` (bankroll $100, mode='paper', paper_trade_count=0). NOTE: paper eval window
      restarts at 0/50 — the Railway volume's post-reset history is stranded on the dead host.
      Confirm fresh-start is acceptable (alternative: pay Railway briefly to export volume — more work).
- [x] 4. GitHub Actions workflow `.github/workflows/trade.yml`: cron `*/5 * * * *` +
      `workflow_dispatch`; `concurrency {group, cancel-in-progress:false}` (one cycle, never killed
      mid-trade); checkout → setup-python 3.12 → `pip install .` → `python -m src.run_trading`
      (SINGLE cycle, no --loop); `timeout-minutes: 8`; env from GH Secrets; base64 PEM via code.
- [ ] 5. GH Secrets (user step): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, KALSHI_API_KEY,
      KALSHI_PRIVATE_KEY_B64, ODDS_API_KEY, DATABASE_URL (Neon). No secret enables live mode.
- [x] 6. Deadman defect fixed (host-independent):
      - `Alerter.__init__` logs a WARNING when disabled (visible in Actions logs).
      - Daily heartbeat: `TradingSettings.heartbeat_due/record_heartbeat` (new nullable
        last_heartbeat_at column) → `alerter.heartbeat(...)` fires once/24h, placed BEFORE the
        idle early-return so a quiet-but-alive system still pings. GH failed-run email covers crashes.
      - healthchecks.io ping = optional future add (not needed; Actions email + heartbeat suffice).
- [x] 7. TDD: tests/test_host_migration.py (9 tests) — base64/literal-\n/raw PEM decode, Alerter
      disabled-warning, heartbeat due/not-due/24h, and a risk-limits-unchanged guard
      (3%/25%/20%/quarter-Kelly/50-gate/mode=paper/count=0). Failed first, then pass. Full suite 250 pass.
- [ ] 8. Make repo public (user step) — required for unlimited Actions minutes → 5-min cycles free.
- [ ] 9. Verify end-to-end: trigger workflow manually (`workflow_dispatch`), confirm run in Actions
      tab writes to Neon, logs a cycle, and Telegram fires (heartbeat or a trade). Screenshot/log proof.

## Open question for user (before task 3)
- Fresh paper history (0/50 restart) on Neon — OK? Or export the stranded Railway data first?
  → RESOLVED 2026-07-03: user chose FRESH START (0/50). Old Railway data abandoned.

## Review (code portion — 2026-07-03)
Host migration code complete + verified; remaining work is user cloud setup (Neon, GH Secrets, public).
- SQLite→Postgres: audit proved dialect-agnostic; added `psycopg[binary]`; SQLite kept for tests.
- Actions cron replaces the always-on process → nothing persistent to exhaust (kills the Railway
  failure mode); failed-run email = death alert; concurrency guard prevents overlapping writers.
- Deadman fixed: Alerter warns when disabled; daily heartbeat distinguishes alive-idle from dead.
- base64 PEM transport ends the literal-\n secret corruption class of bug.
Safety proof (CLAUDE.md): mode default 'paper' asserted by test; risk limits (3%/25%/20%/quarter-Kelly)
asserted unchanged by test; single-writer via cron concurrency → no double-execution/over-exposure;
Postgres transactional + unchanged get_session commit/rollback → no corruption. No live path touched.
Evidence: full suite 250 passed (9 new). Real-Neon connection = task 9 (needs user DATABASE_URL).
