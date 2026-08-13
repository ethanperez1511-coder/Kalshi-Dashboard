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

---

# PHASE 1 — Fix the known bleeders (2026-08-11) — PLAN, NOT YET IMPLEMENTED

Part of a 6-phase upgrade. Phase 1 adds no new money-touching model. Everything here either
corrects wrong math, conserves a scarce resource, or TIGHTENS a match gate (fail-safe direction).

## Safety statement (per CLAUDE.md)
- No Phase 1 code path reads or writes `TradingSettings.mode`, `paper_trades_before_live`, or
  `can_trade_live()`. Paper default structurally preserved.
- Risk ceilings untouched (quarter-Kelly, 3%/trade, 25% exposure, 10% cluster, 5% daily, 20% breaker).
- No mock/fallback data: every new source failure returns None, never a substituted number.
- Schema changes are ADDITIVE ONLY (new nullable columns, new tables). No drops, no rewrites of
  historical rows. Legacy rows keep today's exact behavior.
- All three changes REDUCE the qualifying trade set or make PnL more conservative. Over-exposure
  cannot worsen.

## Bleeder B1 — live settlement PnL is fee-blind
`src/portfolio/tracker.py:66`  →  `fee = kalshi_fee(...) if is_paper else 0.0`
Paper subtracts a simulated entry fee; live subtracts nothing, because the real fee was paid on
Kalshi at entry and never enters the DB. So live `Trade.realized_pnl` overstates by the true fee.
Downstream contamination: `src/portfolio/metrics.py` (win rate, calibration error), the equity
curve, and — the one that matters — the Kelly shrinkage multiplier in `src/risk/kelly.py:16`,
which sizes real money off `realized_pnl`.
Cash itself self-heals in live mode only because `sync_live_bankroll` (`engine.py:43`) overwrites
bankroll from Kalshi each cycle. So this is reporting + sizing corruption, not cash corruption.
Secondary: `_mark_trade_filled` (`engine.py:411`) debits `price*qty/100` with no fee, so the
intra-cycle bankroll used by exposure checks before the next sync runs optimistic.

## Bleeder B2 — the odds cache does not survive the process; quota burns ~288x/day
`src/modeling/odds_api.py:31`  →  `_MODULE_CACHE: Dict[str, tuple] = {}`
Module-level = in-process. The bot now runs as a GitHub Actions cron (`*/5 * * * *`), so EVERY
CYCLE IS A FRESH PYTHON PROCESS and the cache is always empty. The 60-minute TTL is dead code in
production. Burn: 288 runs/day x 3 sports = 864 req/day against a ~500/MONTH free tier. Quota dies
in under a day, then SportsOddsModel silently returns None for the rest of the month.
Second leak: models run BEFORE the filter (`scorer.py:117` dispatch, `:160` filter), so quota is
spent on markets a volume/spread/expiry gate was always going to reject.

## Bleeder B3 — Polymarket matching accepts direction-flipped and negated markets
`src/modeling/models/polymarket.py:72`  →  `if _numbers_in(cand.question) != numbers: continue`
Numeric TOKENS are compared, not the comparator attached to them:
- "CPI above 3%" vs "CPI below 3%" → same tokens, same numbers, sim ~0.9 → MATCHED, and we ingest
  a probability that means the opposite.
- "Will X not happen by 2026" vs "Will X happen by 2026" → `not` is one token of ~8; survives 0.7.
- Bare token sets are symmetric, so "Yankees beat Red Sox" ≡ "Red Sox beat Yankees".
A wrong match is fabricated data pointed straight at the sizing engine. Highest severity in Phase 1.

## Tasks — 1.1 fee-accurate settlement on both paths
- [ ] `Trade.entry_fee: float | None` (dollars, nullable) — src/models/trade.py.
- [ ] `src/database.py:ensure_schema(engine)` — additive migration (PRAGMA table_info on SQLite /
      information_schema on Postgres → ALTER TABLE ADD COLUMN). No Alembic in this repo and
      `create_all` cannot add columns to the existing 134k-row DB or to Neon. Idempotent, never drops.
- [ ] Paper (`_execute_paper`): store `entry_fee = kalshi_fee(qty, price)` at fill time.
- [ ] Live (`_mark_trade_filled`): real fee via `client.get_fills(order_id=...)` →
      `sum(f.fee)/100.0` (KalshiFill.fee is cents, schemas.py:129). On fetch failure fall back to
      the simulated fee and mark it an estimate — never 0.0. Also debit fee intra-cycle.
- [ ] `close_position`: one formula both paths — `gross - (trade.entry_fee ?? kalshi_fee(...))`.
      The `is_paper` branch disappears. `entry_fee is None` (legacy) keeps today's behavior exactly.
- [ ] Tests (tests/test_settlement_fees.py): paper vs live with identical side/price/qty/outcome and
      equal fees settle to IDENTICAL realized_pnl; real N-cent fill fee → `gross - N/100`;
      `get_fills` raising → simulated fallback, never 0.0, no crash; legacy `entry_fee=None` row
      settles exactly as on main; `ensure_schema` idempotent on a populated DB.

## Tasks — 1.2 odds quota: persist, budget, gate, project, fall back
- [ ] PERSIST THE CACHE (the fix that actually matters): table `odds_cache`
      (sport_key, payload_json, fetched_at, source); DB first, module cache second.
      Turns ~864 req/day into ~6.
- [ ] Per-sport TTL: `TRADING_ODDS_TTL_<SPORT>`; default derived from budget
      `ttl_hours = 24 * n_sports * 30 / monthly_cap` (3 sports / 500 cap → ~4.3h, ~210 req/mo).
      Shorter TTL near tip-off, longer pre-game.
- [ ] Budget before spend: table `odds_quota` (month, requests_used, cap); hard stop at cap;
      reuse the existing `QUOTA_DEAD` dark-signal flag.
- [ ] Gate before spend: extract `TradeFilter.prescreen(volume, spread_cents, hours_to_expiry)`
      (market-only gates, no model input) and call it in scorer.py BEFORE model dispatch.
      Prescreen is a strict SUBSET of existing gates — property test proves anything failing
      prescreen also fails `evaluate()`, so trade decisions are provably unchanged.
- [ ] Sport-demand filter: only fetch sport keys some prescreened Kalshi market references.
- [ ] Second source behind an interface: `OddsSource` protocol (`fetch(sport_key) -> [GameOdds]`),
      `TheOddsApiSource` + `EspnOddsSource` (site.api.espn.com scoreboard, free, no key). ESPN
      usually carries one book → de-vigged the same way but confidence capped below the multi-book
      path; `data_sources` records which source paid. Flag `TRADING_ENABLE_ESPN_ODDS` default OFF
      until verified against live responses. If ESPN does not reliably carry moneyline for
      MLB/NBA/NHL, ship interface + NullSource and SAY SO in the report — do not fake a source.
- [ ] Quota API + widget: `GET /api/quota` (used, cap, burn/day, projected month-end, days to
      exhaustion, per-source status) + `QuotaCard.tsx` on Overview.
- [ ] Tests (tests/test_odds_quota.py): cold process + warm DB cache → ZERO HTTP calls (the cron
      regression); budget exhausted → no HTTP, quota_dead set, model returns None; prescreen subset
      property; scorer spends no quota on a prescreen failure; per-sport TTL refetches only the
      expired sport; burn-rate projection against an injected clock.

## Tasks — 1.3 Polymarket: entity match, blocklist, human review queue
- [ ] `src/modeling/entities.py:extract(title) -> MarketEntities`
      {teams_or_persons, dates(resolved), thresholds[(comparator, value, unit)], tickers,
      negated, subject/object order}.
- [ ] `compare(a, b) -> {match | conflict | insufficient}`. ANY conflicting field (opposite
      comparator, different date, flipped negation, swapped subject/object) → hard reject
      regardless of token similarity. `insufficient` → downgrade, never auto-accept.
- [ ] Table `market_match_map` (kalshi_market_id UNIQUE, poly_condition_id, status
      approved|blocked|pending, similarity, entities_json, decided_at). Approved → reuse forever,
      skip fuzzy. Blocked → never match. Pending → NO ESTIMATE (fail closed).
- [ ] `PolymarketModel.estimate`: map first → entity compare → similarity. Uncertain matches are
      enqueued `pending` and return None.
- [ ] API `GET /api/matches/pending`, `POST /api/matches/{id}/approve|block`.
- [ ] Dashboard `Review.tsx` (TypeScript): side-by-side titles, the entity diff that triggered
      review, volume, both prices, approve/block.
- [ ] Tests (tests/test_polymarket_entities.py): "above 3%" vs "below 3%" rejected (regression for
      the live bug); negation flip rejected; subject/object swap rejected; date mismatch rejected;
      approved mapping reused without fuzzy; blocked pair never matched even at sim 1.0; pending →
      estimate() returns None; existing test_polymarket_model.py still green.

## Tasks — 1.4 close-out
- [ ] Full suite green (250 existing + new).
- [ ] PHASE_1_REPORT.md — changes, what is flagged off, manual steps (ESPN verification, review-queue
      approvals; no new API keys required).
- [ ] Append any correction from the user to tasks/lessons.md.

## Lookahead / circularity audit (Phase 1)
- No new probability source → no new circular-pricing risk.
- Prescreen reorders WHEN gates run, never WHAT they decide (subset property test).
- `market_match_map` is keyed on market identity only — stores no outcome, price, or
  post-resolution data, so it cannot leak into a backtest.
- Fee correction uses entry-time data only; no settlement-time info flows backward into sizing.

## Open decisions — RESOLVED 2026-08-11 by user
1. Corrections (fee math, entity conflict-rejection) ship ON. Genuinely new capability
   (ESPN source, prescreen quota gating, review-queue behavior) stays behind flags default OFF.
2. Uncertain Polymarket match → FAIL CLOSED. Pending pair produces no estimate until approved
   in the review queue; approval is remembered forever.
3. ESPN: probe the live endpoint first. Build EspnOddsSource only if moneyline is genuinely
   carried for MLB/NBA/NHL; otherwise ship the OddsSource interface + NullSource and report it.

# PHASE 2 — Weather model (2026-08-11) — PLAN, NOT YET IMPLEMENTED

Requirement from the user, up front: the ensemble-spread-to-probability conversion must be
validated against realized outcomes on historical data BEFORE the weather model may carry any
confidence above the price-derived tier. A miscalibrated weather model looks exactly like edge
until it settles. Calibration evidence goes in the Phase 2 report.

## Findings that reshape this phase (verified live, 2026-08-11)

### F1. We do not ingest weather markets AT ALL. The model would have nothing to score.
- `/markets` first 3000 (the MARKET_FETCH_CAP): categories are General 1549 / Sports 1451.
  **Zero** weather tickers.
- `/events` feed, 2000 markets: "Climate and Weather" = 26 rows, all long-horizon
  (`KXWARMING-50`, `USCLIMATE-2030`, supervolcano/earthquake). **Zero** daily temperature.
- The local DB's 23 "Climate and Weather" rows are all of that long-horizon kind.
- The daily markets exist and are reachable ONLY by explicit series query:
  `GET /markets?series_ticker=KXHIGHNY` →
  `KXHIGHNY-26AUG12-T90 | close 2026-08-13T04:59Z | "Will the high temp in NYC be >90° on Aug 12"`
  Confirmed live for KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDEN, KXHIGHAUS, KXHIGHLAX,
  KXHIGHPHIL (5 open markets each). KXRAINNYC: 0 open.
- So Phase 2 needs a targeted series-ingest path before any modelling. This is the prerequisite.

### F2. Thresholds come in BOTH directions.
Austin's example is "**<**99°" while NYC's is "**>**90°". A parser that assumes `>` inverts the
question — the same class of failure as the Polymarket direction bug in Phase 1.3.

### F3. Daily markets make close_time load-bearing.
These close ~05:00Z the following day. The 14-day expiry filter and the 30-minute stale-snapshot
guard both key off dates that, for these contracts, turn over every single day.

## Tasks — 2.0 prerequisite: reach the markets
- [ ] Series-targeted ingest: `KalshiClient.get_series_markets(series_ticker)`, config
      `TRADING_WEATHER_SERIES` (the 7 confirmed tickers), wired into live_ingest alongside the
      existing feeds. Flagged, default ON for ingest only — ingesting is not trading.
- [ ] Ticker parser → (city, date, threshold_f, direction). Round-trip tested against real
      tickers, including the `<` variant. Unparseable ticker → no estimate, never a guess.
- [ ] City → (lat/lon, NWS settlement station) map, from the contract rules text, not assumed.

## Tasks — 2.1 data layer (free, no key)
- [ ] Open-Meteo ensemble client → members for daily max temp. DB-cached like odds
      (cron = fresh process every 5 min; a module cache is inert — lesson L3).
- [ ] Realized-outcome client for scoring (NWS station observations; Open-Meteo archive as the
      bulk source if parity holds — see 2.3).
- [ ] Open-Meteo publishes a free-tier daily call limit; budget against it with the same
      ledger pattern as the odds quota.

## Tasks — 2.2 model
- [ ] Ensemble members → empirical CDF → P(max temp beats threshold), respecting direction.
- [ ] Calibration layer. Raw ensembles are known to be under-dispersed, which produces
      overconfident tail probabilities — precisely the failure that reads as edge. Plan is
      NGR/EMOS (μ = a + b·ens_mean, σ² = c + d·ens_var) fitted on historical pairs, walk-forward.
- [ ] Coherence check, free from the market structure: within one city-day the thresholds are
      ordered, so P must be monotone across them. A violation means the model is broken; assert it.

## Tasks — 2.3 calibration validation — THE GATE
- [ ] Build a (forecast at lead time L, realized outcome) dataset over N past days × 7 cities.
      Feasibility is being probed now; the honest fork:
      (a) historical ENSEMBLE forecasts retrievable → fit and validate NGR properly;
      (b) only deterministic historical forecasts → validate a spread proxy, and say plainly in
          the report that dispersion is estimated rather than observed;
      (c) neither → the model does NOT get promoted, and ships confidence-capped at the
          price-derived tier (i.e. untradeable) with that stated as the reason.
- [ ] LOOKAHEAD IS THE MAIN RISK. Reanalysis may be used ONLY as the outcome, never as an
      input. Calibration fitted strictly on data preceding each evaluation window. Audit this
      explicitly and show the audit.
- [ ] Metrics: Brier score, Brier skill score vs a climatology baseline, reliability diagram
      (bucketed predicted-p vs realized frequency), PIT histogram, CRPS.
- [ ] Promotion rule, encoded in code not prose: confidence stays at the price-derived tier
      until BSS > 0 vs climatology AND the reliability slope is within tolerance on HELD-OUT
      data. Config `WEATHER_CONFIDENCE_PROMOTED` default OFF; the report carries the evidence.

## Tasks — 2.4 settlement parity (silent edge-killer)
- [ ] Kalshi settles on a specific official station observation. If we model a grid value that
      differs from that station by even ~1°F, every threshold near the line is mispriced while
      looking correct. Measure the discrepancy over a real sample; if material, model the
      station series directly rather than the grid.

## Safety statement (per CLAUDE.md)
- No task touches `mode`, the 50-trade gate, or `can_trade_live()`.
- Risk ceilings untouched; the weather model is an input to the existing EV filter and risk
  layer, not a bypass of either.
- Fails safe: unparseable ticker, missing ensemble, failed coherence check, or unvalidated
  calibration all produce NO estimate rather than a substituted number.
- New capability ships behind flags default OFF (lesson L5); the ingest path is the exception
  and is ingest-only.

## Carried decisions (recorded so they cannot be lost)

### Polymarket re-entry is the arb scanner, not the price model
Phase 1.5 took Polymarket coverage to zero: every matched pair failed on resolution horizon.
The re-entry path is the Phase 2 cross-exchange arb scanner, under these restrictions:
- **Event-dated contracts only** — game dates, election dates. The horizon-convention gap that
  killed the price model exists precisely where the "event" is open-ended ("next PM, ever"). It
  cannot exist when both venues resolve on one dated real-world occurrence.
- **Verify resolution-date agreement explicitly, per pair.** Not a category assumption, not a
  series-level rule: each pair proves its two contracts settle on the same dated event before it
  is eligible. Same discipline as the entity check, applied to dates.
- Exact entity match only, no fuzzy — an arb position is exposed on BOTH legs, so a wrong match
  loses twice rather than being merely uninformative.
- Reminder from the Swift ruling: same event + same date is still not enough if the two venues
  use different evidentiary standards. The arb scanner needs the resolution-criteria check too.

### Per-model trade counts (implemented in 2.0)
With Polymarket at zero and the price-derived models gated off, the paper sample is
SportsOdds-only. The 50-trade gate counts trades, not evidence, so it can read 50/50 while every
other model has zero settled trades — "validated" about a system validated in one corner.
`src/portfolio/attribution.py` now tracks placed and settled counts per model, surfaced in the
daily digest, and `models_without_settled_evidence()` names the models the record cannot speak
for. **Enforcing a per-model minimum before live is a policy decision and is NOT implemented** —
flagging it rather than quietly changing what the gate means.

## Review — 2.0 COMPLETE 2026-08-11

Suite: **389 passed** (357 → 389, 32 new). Verified end to end against the live Kalshi API.

**Series ingest.** `get_series_markets()` on its own call path; config-driven via
`TRADING_INGEST_SERIES_TICKERS` (7 cities as the default, not hardcoded); own `SERIES_FETCH_CAP`,
so it touches neither `MARKET_FETCH_CAP` nor the odds quota. One failing series is caught and the
rest continue; an empty series is distinguishable from a failed one.

**Terms parsed, never assumed.** Kalshi publishes `strike_type` + `floor_strike`/`cap_strike`, so
those are the source of truth, with the human-readable subtitle as an independent cross-check.
Stored explicitly on the market row (`strike_direction`, `strike_value`, `strike_unit`,
`terms_status`). Direction is per-CONTRACT: NYC lists both `>90°` and `<83°` for the same day, so
the earlier "Austin is a < city" framing was wrong and a per-city rule would have mispriced half
the book.

**Boundary semantics.** `floor_strike=90` + subtitle "91° or above" ⇒ YES iff T ≥ 91, i.e.
STRICTLY greater. Both directions strict, and the subtitle cross-check rejects any contract whose
text implies a different convention — an off-by-one here moves mass at the money.

**Refusal over defaulting.** Unreadable → `terms_status="unparsed"`, direction and value stay
NULL. Structured terms that exist but cannot be used never fall back to the title: if the API
says `between` and the title says ">90°", the title is a lossy summary and using it would price a
range contract as one-sided.

**Live evidence (2026-08-11):** 84 contracts fetched across 7 series → 28 priceable
(14 above / 14 below), 56 unsupported, **0 unreadable**, 100% of readable contracts parsed.

**Finding that changes 2.1 scope.** 56 of 84 live contracts are `between` buckets. Each city-day
is a complete partition: `<84`, `[84,85]`, `[86,87]`, `[88,89]`, `[90,91]`, `>91`. Two consequences:
- One-sided thresholds are only ~33% of the book; interval support is where the coverage is.
- The partition sums to 1, which is a far stronger model-integrity check than the monotonicity
  test originally planned — and it is free.
Initially these were counted as parse failures, which made the coverage metric lie (a modelling
gap reported as a broken parser). Now a distinct `TERMS_UNSUPPORTED` state.

## Decisions taken 2026-08-11 (after the weather-API probe)

### Data source: NWS primary, Open-Meteo paper-only
Open-Meteo's free tier is CC-BY-4.0 **non-commercial** — unacceptable behind real capital. NWS is
US-government public domain AND is the settlement source, so forecast and truth come from one
provider. Confirmed from the contract text itself: every series settles on the **NWS
Climatological Report (Daily)** at a named station — Central Park, **Chicago MIDWAY (not
O'Hare)**, Miami International, Denver, Austin Bergstrom, LA Airport, Philadelphia International.
The rules also state "is greater than 90°" / "is less than 99°" verbatim, independently
confirming the strict-inequality boundary the 2.0 parser encodes.
Open-Meteo stays for backfill/research while in paper mode. Resolve before the live flip.

### Model architecture: deterministic forecast + σ fitted per lead
Not a shortcut — it is the only architecture NWS supports (deterministic temperature only; the
sole probabilistic fields are precipitation/thunder/wind). Historical ensemble members are not
retrievable from Open-Meteo either: a rolling ~3–4 day window that fails **silently**, returning
HTTP 200 with null members. Any ingest asserts non-null and documented member count rather than
trusting the status code.
Baseline is already validated end to end: fit Jun–Jul 2025, out-of-sample Aug 2025, 549
forecast-strike pairs → **Brier 0.0994 vs climatology 0.2475, skill 0.598**; tails well
calibrated, mid-range slightly overconfident (variance inflation).

### Phase 2 report must state plainly
Dispersion is **estimated from historical forecast error, not observed**. Flow-dependent
uncertainty — knowing a confident day from an uncertain one — is the known gap, and the ensemble
challenger below is the planned fix. This goes in the report as a limitation, not a footnote.

## Tasks — 2.1 (current)
- [ ] `src/weather/stations.py` — series → (station, lat/lon, timezone). Config map, NOT inferred,
      with a test asserting the live rules text still names the expected station so a Kalshi change
      breaks a test instead of silently mispricing.
- [ ] NWS client. User-Agent is MANDATORY (403 Access Denied without one, not a 400). Deterministic
      gridpoint forecast + the CLI product as settlement truth.
- [ ] Truth series from the **CLI product**, not station observations. Measured gap, Open-Meteo grid
      vs KNYC ASOS: mean bias +1.50 °F, MAE 1.70, max 3.3 — and settlement is CLI, so ASOS is
      itself a proxy. Against 1-degree buckets a 1.5 °F bias dominates every modelling refinement.
- [ ] Model: P(T > strike) from a normal around the deterministic forecast with σ fitted per
      (station, lead). Integer settlement + strict inequality ⇒ P(T ≥ strike + 1).
- [ ] Calibration harness: walk-forward, fitted strictly on data preceding each evaluation window.
      Brier, Brier skill vs climatology, reliability bins, PIT.
- [ ] Promotion gate IN CODE next to the climatology gate: confidence stays at the price-derived
      tier until BSS > 0 vs climatology and reliability slope within tolerance on HELD-OUT data.

## Tasks — 2.2 GEFS ensemble PROBE (gate before any build)
Do NOT build the dynamical.org path yet. Probe first, same discipline as the ESPN and Open-Meteo
probes. Acceptance criteria, all required:
- [ ] Archive contains real forecast-as-issued members — member count as documented (GEFS 31,
      ECMWF IFS ENS 51), not silently truncated.
- [ ] RMSE grows monotonically with lead time. Reanalysis cannot do this, so it is the test that
      the data is a forecast and not a hindcast — the same check that validated
      `temperature_2m_previous_dayN`.
- [ ] No silent-null failure mode. Out-of-range requests must error, not return 200 with nulls.
- [ ] Confirm the Python 3.11+ (zarr v3) constraint is CI-only. CI runs 3.12; this machine is
      3.9.6. Nothing in local tooling or the test suite may depend on it before that is settled.
- [ ] Licence check on the hosting terms, not just on NOAA's underlying public-domain data.

Only if the probe passes does GEFS get built — and then as a **scored challenger**: same held-out
window, and it must BEAT the deterministic baseline's Brier skill to be promoted. That bar is
recorded in code beside the climatology gate, so promotion is a measurement rather than an
opinion.

## Review
_(filled after implementation, with the calibration evidence)_

---

## Review — COMPLETE 2026-08-11 (full detail in PHASE_1_REPORT.md)

Suite: **325 passed** (250 before, 75 new). Frontend `tsc --noEmit` clean. FastAPI boots with
the two new routers against the real DB.

**1.1 fee accounting.** `Trade.entry_fee` + `entry_fee_source` recorded at fill (paper:
simulated; live: real fee from `get_fills`, falling back to an estimate but never 0.0).
Settlement uses one formula for both paths — the `is_paper` branch is gone from the PnL math.
Found a SECOND bug while fixing: the live path debited the entry cost at fill and then credited
`realized_pnl` (which contains that cost) at settlement — double-subtraction, masked by
`sync_live_bankroll`. Fixed by moving the `is_paper` split to the cash ledger where it belongs:
paper = equity-at-cost, moves once at settlement; live = real cash, cost+fee at fill, payout at
settlement. Both net `gross - fee`.
`ensure_schema()` added (no Alembic in repo; `create_all` cannot add columns). Rehearsed on a
copy of the real 95MB DB: 11 trades / 6 positions / 134,950 markets in and out, legacy rows
null, rerun a no-op. Also caught a pre-existing gap: `trading_settings.last_heartbeat_at` was
missing from any pre-July database.
Stale test `test_live_position_no_simulated_fee` superseded — it asserted the bug.

**1.2 odds quota.** Root cause was worse than the free-tier size: the cache was a module-level
dict, and the July move to a per-cycle Actions cron made it permanently empty — ~864 req/day
against a ~500/month cap, TTL dead code. Cache moved to the DB (`odds_cache`), TTL derived from
the budget (720h x n_sports / cap = ~4.3h, spends the cap exactly), ledger (`odds_quota`)
charged before each request, `TradeFilter.prescreen()` for gating before model dispatch,
`OddsSource` interface + ESPN fallback, `GET /api/quota` + QuotaCard.
ESPN probed live before building: moneyline present for MLB/NBA/NHL but ONE book (DraftKings)
and day-of-game only, so it ships default OFF and confidence-capped at 0.70. Three probe
findings are each locked by a test — explicit `?dates=`, missing `odds` key on FINAL games, and
the Akamai bot manager that 403s custom User-Agents.
Regression test: cold process + warm DB cache makes ZERO HTTP calls.
Prescreen proven decision-neutral by a grid property test.

**1.3 Polymarket entities.** Demonstrated the live bug with real phrasing: "CPI above 3%" vs
"CPI below 3%" scores 0.818 similarity with identical numeric tokens and MATCHED. Added
`entities.py` (direction, magnitude, dates, negation, party order), `market_match_map`
(approved/blocked/pending, fail-closed), review API + Review page.
Then ran old vs new against 2,055 real Kalshi markets + 1,500 live Polymarket markets, which
exposed a SECOND bug in my own first cut: "Alexandru Rafila" matched "Alexandru Nazare" — two
different candidates — because I compared entity phrases and the intersection was non-empty.
Accented names ("Cătălin") were mangled by the ASCII regex. Fixed with Unicode-aware extraction
and per-token containment.
Result on live data: 17 priced before -> 13 priced, 4 stopped. Two were different people (real
saves); two are judgment calls (Eisenkot/Eizenkot transliteration, Taylor Swift wedding pair)
now queued for review instead of traded blind.

**Safety proof.** No Phase 1 code path reads or writes `mode`, `paper_trades_before_live`, or
`can_trade_live()`. Risk ceilings untouched and still asserted by test_host_migration.py.
Schema changes additive only. All three changes reduce the tradeable set or make PnL more
conservative, so over-exposure cannot worsen. Real DB verified post-migration: counts
unchanged, `PRAGMA integrity_check` ok, mode still `paper`, 11/50.

**Flagged for a decision, not silently fixed:** `sync_live_bankroll` sets bankroll to Kalshi
*cash*, while paper bankroll is equity-at-cost. Risk limits divide exposure by bankroll, so the
25% cap binds earlier in live than in paper. Fail-safe direction, but it means the 50-trade
paper evaluation does not transfer cleanly to live sizing. Predates this phase; recommend
resolving before the live switch.

**Deliberately not built:** sport-demand filtering (derived TTL already spends exactly the cap),
live/pre-game TTL split (same budget), ESPN summary/core endpoints (only needed for Phase 4 CLV
backfill).

## Queue — not urgent, tracked

- [ ] **Weather classifier net is too wide.** `KXPERFORMROLE007-MONEYPENNY-JUN` (a
      James Bond casting market) reached the WEATHER terms parser and was marked
      unpriceable. Harmless today — it refused safely rather than pricing
      something it could not read — but `is_temperature_market()` keys on
      `"temp"` appearing anywhere in the title, and "contemporary", "attempt",
      "temporary" and similar all match. Narrow it to the temperature series /
      structured strike shape rather than a substring, and add the Bond ticker
      as a regression fixture.

## BLOCKED ON APPROVAL — correlated-cluster key collapses seven independent cities

`_extract_cluster_key` returns `market_id.split("-")[1]`. Its own docstring says
"For non-MVE tickers, fall back to the full market_id" and **the code does not
do that**. Demonstrated:

    KXHIGHNY-26AUG13-T92   -> '26AUG13'
    KXHIGHAUS-26AUG13-T99  -> '26AUG13'
    KXHIGHMIA-26AUG13-T88  -> '26AUG13'
    KXMVESPORTSMULTIGAMEEXTENDED-S2026XXXX-YYYY -> 'S2026XXXX'   (correct)

So every weather contract on the same date, across all seven cities, shares one
cluster and one $10 cap (10% of a $100 bankroll). New York and Miami weather are
not correlated. Meanwhile the thing that IS correlated — the six-contract ladder
on one city-day — is grouped only incidentally, via the shared date.

Wrong in both directions, and it caps the weather model at roughly three
concurrent positions across the entire book.

**Not changing this unilaterally.** It is the risk layer, and the fix loosens an
effective constraint. Proposal for approval:

- cluster key = `parts[0] + "-" + parts[1]` for non-MVE tickers, so a city-day
  ladder clusters and two cities do not. Keep `parts[1]` for MVE tickers, which
  is the case the function was written for.
- Assert the four hard limits still hold afterwards: quarter-Kelly, 3% per
  trade, 25% total exposure, 20% drawdown breaker. The cluster cap is the only
  number that moves.
- Test that a six-contract NYC ladder still shares one cluster, that NYC and
  Miami do not, and that an MVE parlay's legs still share theirs — each
  demonstrated failing against the current key first.
