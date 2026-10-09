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

## Trade 1/50 autopsy — three findings, in severity order (2026-08-13)

Stored: `KXHIGHCHI-26AUG13-T76 | WeatherModel | NO | qty 3 | price 92 |
p_model 0.0571 | edge -0.0329`. Every number below reproduces exactly by
re-running the real gate code on inputs solved from the stored row:
`last_price=9, yes_bid=8, yes_ask=9, confidence=0.85`.

- [x] **F1. `no_ev` is computed with the win and loss amounts swapped.**
  `raw_ev_no = (1-p)*price_no - p*(1-price_no)`, which algebraically equals
  `price_no - p`. Correct is `(1-p)*(1-price_no) - p*price_no`, i.e.
  `(1-p) - price_no`, which is exactly `no_edge` — the same identity the YES
  side already satisfies. Verified against 400k-trial Monte Carlo: at p=0.30
  with NO at 80c the true EV is -0.10 and the code reports +0.50. The error
  grows with how expensive NO is, so it systematically manufactures enormous
  fake EV on precisely the cheap-YES longshot fades this system trades, and
  drags `recommended_side` to NO with it. Gate 1 (`best_ev <= 0`) therefore
  never bound on this trade class.
- [x] **F2. The EV is evaluated at a different price than the fill.**
  `ORDER_TYPE` defaults to `"maker"`, so `calculate_ev` ignores bid/ask and
  prices NO at `100 - last_price` = 91c. `_compute_fill_price` with
  `PAPER_CONSERVATIVE_FILLS` fills at `100 - yes_bid` = 92c. One cent, and it
  flipped the decision: NO edge is +0.0329 at 91c (passes the 0.03 tier) and
  +0.0229 at 92c (fails it). Fixed by giving evaluation and execution one
  shared fill-price function so they cannot diverge again.
- [x] **F3. The stored edge is not the edge that was gated.**
  `edge` stores `edge_yes` (-0.0329); the filter gates `best_edge`, the
  recommended side's edge (+0.0329). Both are now stored, plus the fill price
  the EV was computed against, so an autopsy is a lookup.

Not a bug, but a design mismatch to rule on: `_get_edge_threshold` returns
**0.03** at confidence >= 0.7, 0.05 for 0.4-0.7, 0.08 below. The 5% floor is
the middle tier, not the high-confidence one. Trade 1/50 passed on the 0.03
tier by 0.0029.


## Phase 4 sweep list — first-class experiment parameters

- **Edge-threshold ladder** (`TradeFilter._get_edge_threshold`): 3% / 5% / 8%
  descending with confidence. Ruled 2026-08-14 to keep as coded; it is now a
  named decision with its rationale at the definition. Sweep it as a parameter,
  not as a bugfix. Revisit condition: realized losses concentrating in the 3%
  tier. Readable directly from `trades.traded_edge` — no reconstruction needed.

---

# Weekend triage: DB bloat, settlement-source change, weather blackout, silent alerts (2026-08-16)

## Status: INVESTIGATION COMPLETE for 2/3/4, BLOCKED on production data for 1.
Phase 3 stays paused. No code changed yet.

## Safety statement (per CLAUDE.md)
- Nothing proposed here touches `mode`, `paper_trading_mode`, or `can_trade_live`.
  Paper default preserved; the live path is not reachable by any item below.
- Risk limits untouched: quarter-Kelly, 3%/trade, 25% exposure, 20% breaker.
- P1 fixes DELETE rows from bookkeeping tables only (markets/snapshots). Positions,
  trades and opportunities are never pruned by anything proposed here.
- P2 and P3 can only REDUCE the set of markets that price. Neither can open a trade.
- Every diagnostic run so far has been read-only: SELECT-only, GET-only.

## Evidence collected 2026-08-16 (all reproducible)

### P2 — settlement source: CONFIRMED CHANGED, ALL SEVEN SERIES
`tasks/diag_rules.py` against the live API. Every series now reads
"...according to The Weather Company", CLI code retained in the parenthetical:
CLINYC, CLIMDW, CLIMIA, CLIDEN, CLIAUS, CLILAX, CLIPHL. Zero of seven still say
"climatological report"; six of seven no longer contain their `rules_marker`
(KXHIGHDEN passes only by the coincidence that "Denver" appears in both).

Kalshi's own series API carries the notice, verbatim:
> "Effective Friday, August 14th, daily temperature markets will transition
> their settlement source from the National Weather Service (NWS) to The
> Weather Company. The Weather Company utilizes NWS as its primary underlying
> source, and official settlement data will be accessible at
> https://weather.com/kalshi."

Answer to (b) — the settlement NUMBER is unchanged, and this is measured, not
assumed. The feed behind weather.com/kalshi is
`GET https://weather.com/kalshi/api/climate/primary?date=YYYY-MM-DD` (no auth).
Its domestic records carry `cliId`, `issueTime` and a
`official|preliminary|no_report` status — the CLI product's own vocabulary —
while the *international* endpoint returns `source:"TWC"` with
`observationCount:24`. Two pipelines: a CLI parser for US stations, TWC's own
aggregation abroad. Our seven are all on the CLI-parser path. Max/min compared
against the live NWS CLI products for all 7 cities on 2026-08-14 and 08-15:
**14/14 exact, zero divergence.**

So the calibration chain's TARGET is intact; MOS/GHCN fits do not need
re-validation against a different number. What is missing is any means of
NOTICING if that ever stops being true. "Primary underlying source" is not
"only source", and the certified rulebook (GLOBALTEMPERATURE.pdf, last modified
2025-12-12) still names NWS as Source Agency while the market rules name TWC.

Answer to (c) — **weather cells are NOT refusing because of this, and would not
have.** The station guard is a `@pytest.mark.live` test, CI-only. Nothing in
the scoring path reads `rules` text. `is_temperature_market` matches on TITLE,
`is_in_scope` on the ticker->station map; both still pass. Verified live with
`tasks/diag_terms.py`: 28 contracts parsed, 56 correctly `unsupported`
(`between` ladders), 0 unparsed, across all 7 series. Had the real cause been
absent, this system would have kept pricing every city with no idea the
settlement authority had changed.

### P3 — WeatherModel 68 -> 0: NOT downstream of P2
Reproduced live: at 17:25 UTC every station returned
`MosUnavailable: HTTP 404` for the 12Z run; at 17:32 UTC the same call returned
`MosForecast(KNYC, 2026-08-17, lead 1, 83.0F)`. The 12Z MEX run had not yet
landed in the IEM archive.

The structural problem this exposes: `run_time_for(target, lead)` returns
`target - lead` days at 12Z, and `lead` is computed as `target - today`. Those
cancel — **every lead always demands TODAY's 12Z run.** Leads 2 and 3 do not
fall back to older runs that are certainly published. So from 00:00 UTC until
the 12Z MEX run lands (~17:30 UTC), all 7 stations x all leads refuse as
`mos_unavailable`, which is roughly 73% of the 5-minute cycles in a day.

- [ ] NOT YET CONFIRMED as the production cause: needs the `WeatherModel
      refusals:` line from a failing cycle's funnel output. If it reads
      `mos_unavailable=68` this is settled; if it reads `cell_...` the cause is
      a stale/unpromoted fit and the refit job is the thing to chase.

### P4 — the alert that did not fire
`.github/workflows/live-checks.yml:43-49` has `if: failure()` and the correct
secret names (identical to trade.yml, which does deliver). So the step ran.
`src/alert_live_failure.py:23-32` calls `alerter.send(...)` and **discards the
return value**, then returns 0 unconditionally. Commit 7089853 changed
`Alerter.send` to return a delivery bool precisely so this was countable, and
updated the high-frequency callers — it never updated this one, the
lowest-frequency and highest-consequence caller. A disabled or refused send is
therefore indistinguishable from a delivered one, from both Telegram and the
Actions UI: green step, no message.
Same file is the alert path for retention.yml, book-recorder.yml and
weather-refit.yml — all four scheduled workflows share the defect.
`alert_live_failure` is also absent from `TestEveryEntrypointExecutes` in
tests/test_pipeline_entrypoint.py, so L27 applies unchanged.

### P1 — DB trajectory: BLOCKED, hypotheses ranked
Local `.env` DATABASE_URL is `sqlite:///kalshi.db`; production is a GitHub
secret, and neither `gh` nor `psql` is installed. No production query has been
run, so nothing below is confirmed.
Ruled out by reading the code: `KalshiMarket.close_time` is a REQUIRED field,
so ingest cannot write a NULL `close_date`; the `close_date IS NULL` arm of
`open_market_count` is unreachable from ingest. `markets.market_id` carries a
unique index and `sync_markets` dedupes within the batch, so duplicate rows per
ticker are not the mechanism either.
Leading hypothesis: the markets table is an unbounded graveyard. Nothing ever
deletes a market row; retention prunes `price_snapshots`/`orderbook_deltas`
only. Each cycle unions in whatever the capped walks return
(MARKET_FETCH_CAP 3000 + EVENT_FETCH_CAP 2000 + SERIES_FETCH_CAP 500, every 5
minutes), and every ticker with a far-future `close_date` counts as "open"
forever whether or not it is tradeable. 32,074 open on 2026-08-13 (measured, in
the expire_markets docstring) -> 328,099 today is ~100k rows/day.

## Tasks
- [x] P1.1 Read-only `db-stats` maintenance action: per-table bytes and row
      counts, markets by status, open-count broken out by close_date horizon,
      first-seen-per-day histogram, snapshot span. Answers this question now
      and every future time without a local DATABASE_URL.
- [ ] P1.2 Confirm whether retention has ever run in production (Actions history
      + a `last_pruned_at` marker row so the answer is in the DB, not only in a
      UI that ages out).
- [ ] P1.3 Fix the accumulation at its root once measured. Candidate: an
      ingest-horizon filter (do not store markets closing beyond N days — the
      velocity limit already refuses to trade them) plus a markets retention
      sweep for rows long past close with no position and no trade history.
      Must not delete any market referenced by a position, trade or opportunity.
- [ ] P2.1 Re-point the station guard: match the CLI code (`clinyc`, `climdw`,
      ...) which identifies the observing site and is stable across vendors,
      plus assert the named settlement source. Replace the
      "climatological report" assertion rather than deleting it.
- [ ] P2.2 Guard reports ALL seven series, not just the first to fail. The
      current loop aborted on KXHIGHNY and never revealed that all seven had
      moved — three days of failures that undercounted the blast radius by 7x.
- [ ] P2.3 Add the real detector: a daily live check comparing
      weather.com/kalshi `climate/primary` against the NWS CLI/GHCN value for
      each of the 7 stations. Divergence is the event that actually matters,
      and it is the only thing that would tell us TWC stopped being a CLI
      passthrough. Whole-degree exact match; any mismatch fails loudly.
- [ ] P2.4 If the TWC feed is ingested at all: hard-fail on `data: null` /
      `no_report` (HTTP 200 with null payload) and gate on
      `status == "official"`. Never coerce to zero. Note its history begins
      2026-06-01, so GHCN remains the fitting truth source.
- [ ] P2.5 Record in the code WHY the fits were not re-validated: 14/14 CLI
      match on 2026-08-14/15, with the check that keeps proving it (P2.3).
- [ ] P3.1 Get the production refusal counters and confirm or replace the
      MOS-blackout diagnosis before writing a line of fix.
- [ ] P3.2 If confirmed: use the most recent PUBLISHED 12Z run and derive
      `lead` from that run's date, instead of demanding a run that may not
      exist yet. Predictor stays MEX 12Z — the same product sigma was fitted
      on — and the fit loaded is the one for the lead actually used. No
      fallback to a different model, ever.
- [ ] P3.3 Alert when a whole model goes to zero priced for N consecutive
      cycles. 68 -> 0 for a weekend should not need a human to notice it.
- [x] P4.1 `alert_live_failure` checks the send result; on non-delivery write to
      GITHUB_STEP_SUMMARY and exit non-zero so the step goes red. The job is
      already red, so this cannot mask the original failure — it can only stop
      an undelivered alert from looking delivered.
- [x] P4.2 runpy entry-point test for `src.alert_live_failure`, executed the way
      Actions executes it, covering delivered / refused / no-credentials.
- [ ] P4.3 Extend to the class: assert every scheduled workflow has a failure
      alert step with `if: failure()`, so the next workflow added cannot ship
      without one. trade.yml and maintenance.yml currently have none.

## Review

### P4 — landed 2026-08-16
`src/alert_live_failure.py` now uses `send()`'s delivery bool as its exit code:
delivered -> 0 with a run-summary line, refused/disabled -> run summary marked
failed, message to stderr, exit 1. The step runs only under `if: failure()`, so
the job is already red and nothing is masked; what changes is that "alerted"
and "alerted nobody" stop looking identical.

`tests/test_live_failure_alert.py`, 8 tests, TDD — 6 failed against the old
code, all 8 pass now. Every one executes the entry point through
`runpy.run_module(..., run_name="__main__")` with `src.alerts` patched at the
source module (L27), including the real disabled `Alerter` with no credentials,
which is the exact production scenario. The regression is pinned directly:
`test_delivered_and_refused_do_not_share_an_exit_code`.

### P1.1 — landed 2026-08-16
`src/maintenance/db_stats.py` + `python -m src.maintenance --db-stats`, wired to
a `db_stats` boolean input on maintenance.yml. SELECT-only, no confirmation
token, and checked before every destructive branch in both the workflow and
`main()`.

`tests/test_db_stats.py`, 8 tests. The two that matter: the report imports
`open_market_count` rather than re-deriving it, so the census and the funnel
cannot disagree (L26); and `test_collect_writes_nothing` fingerprints row counts
and market rows before and after.

Smoke-run against a copy of the local 95 MB SQLite DB (2026-08-12 vintage,
predates expire_markets):

    open markets (funnel) : 1978
    open status past close: 132925
    scorer can reach      : 0   (snapshot < 30min)
    open by close horizon : <=7d 0 | 8-30d 2 | 31-90d 6 | >90d 1970
    top open prefixes     : KXGDPYEAR 56, KXTRUMPPARDONS 52, KXPERFORMROLE007 50...

1,970 of 1,978 "open" markets close beyond 90 days, against a 14-day velocity
limit — none of them is ever tradeable. That is the graveyard hypothesis showing
its shape on real data, though on a stale local DB rather than production.

Full suite: 813 passed, 2 deselected (live).

### Recorder triage — landed 2026-08-16 (both bugs)

**R1. SSL death no longer costs the hour.** Three compounding defects, all fixed:
`get_engine` now sets `pool_pre_ping=True` and `pool_recycle=300` for non-SQLite
URLs, so a connection Neon closed while idle is proved dead and replaced before
it is handed out. `_flush` no longer clears the buffer before the insert — it
used to, so a failed write lost the batch even in principle and a retry had
nothing to retry. Writes go through `_attempt_write`, which retries
WRITE_ATTEMPTS=3 times, disposes the pool between attempts (retrying with the
same dead connection just fails identically), and NEVER raises. That last part
is the actual crash: the OperationalError escaped `_flush`, the reconnect
handler caught it and called `_flush` again, and the second raise was outside
the try and ended a 55-minute unbackfillable recording window.
A batch abandoned after all retries is counted (`write_failures`,
`messages_lost`) and logged at ERROR into the run summary — losing a batch is
survivable, losing it silently is not.

**R2. The subscribe list can no longer contain corpses.** `markets_to_record`
now joins `markets`, requires status in OPEN_STATUSES and `close_date > now`,
and bounds opportunities to the last RECENT_OPPORTUNITY_HOURS=6 so the list
refreshes per run instead of accumulating every market ever scored. A candidate
with no `markets` row is dropped: unknown liveness is not a licence, and those
subscriptions are where the blank-ticker rows came from. Held positions still
sort first among live markets, but a position in a closed market is not
recorded — there is no book on a settled market, and settlement reads the
exchange, never this feed.

**R3. The day-7 clock now counts live hours only.** `is_live(received_at,
close_date)` is defined once in `src/recorder/health.py` and used by BOTH
`recorder_health` (coverage hours) and `day7.measure` (trade prints), so the
hours and the prints can never describe different samples. Every recorded row
classifies as live / dead / unattributed; the buckets are asserted to sum to
the total. Dead rows are reported, never deleted — their share IS the answer to
how much of the record was real.

Tests: 13 in test_recorder_resilience.py, 8 in test_recorder_liveness.py, 2
added to test_db_stats.py. TDD throughout — 13 and 6 failed first respectively.
Full suite 836 passed.

Four pre-existing tests updated, deliberately: TestMarketSelection (x3) and
TestRecorderSubscribeList (x1) seeded positions and opportunities with NO
`markets` row, which production never produces — a scored or held market was
ingested by definition. They encoded the old permissive behaviour, not a
requirement. Fixtures brought to production shape; the assertions are unchanged.

**Constraint this places on the graveyard fix (P1.3):** archival must never
remove a `markets` row for a market with an open position or an opportunity
inside the recency window, or the recorder goes blind on exactly the markets it
most needs to tape. Archive, never delete — as instructed.

### Still open
P1 root cause, P2 and P3 all wait on production numbers. Needed:
1. maintenance workflow dispatched with `db_stats: true`. NOW PUSHED (see L28 —
   it was never committed, which is why the checkbox did not exist). The census
   now also carries the recorder live/dead/unattributed split, so one dispatch
   answers both the storage question and the day-7 coverage question.
2. Retention: ANSWERED — 4/4 green, working. Telegram: ANSWERED — trade alerts
   arrive, so the token is valid, which makes the missing live-checks alert
   MORE puzzling, not less: same secret names, `if: failure()` present, working
   channel. The fix makes the next one self-reporting either way, but the
   reason this one vanished is still unexplained. Still worth a look at that
   run's "Alert on failure" step.
3. The `WeatherModel refusals:` line from any recent trade.yml cycle log.


---

# Storage emergency: parlay mint (2026-08-17)

## Census findings (operator-supplied)
376k open markets, of which 218k KXMVECROSSCATEGORY + 156k
KXMVESPORTSMULTIGAMEEXTENDED = 374k. 123k new rows on 08-15 alone. Scorer
reaches 2,270. ~60 MB/day against 126 MB headroom = ~2 days to the cap.
Recorder liveness: 194 dead of 253,261 (~0%) — 56h live coverage stands.

## Safety statement (per CLAUDE.md)
- Nothing here touches `mode`, `paper_trading_mode` or `can_trade_live`.
- Risk limits untouched: quarter-Kelly, 3%/trade, 25% exposure, 20% breaker.
- The ingest filter can only REDUCE what is persisted. The seven weather series
  are asserted by test never to match an exclusion.
- The purge never touches a market with an open position or an opportunity
  inside the recorder's recency window — the constraint carried over from the
  recorder fix. Positions, trades and opportunities are never deleted by it.
- Deletion is limited to market rows with zero dependent rows in ALL of
  price_snapshots, trades, positions, opportunities, orderbook_delta_raw.

## Tasks
- [x] 1a. Ingest exclusion, config-driven (`TRADING_EXCLUDED_SERIES`), applied
      before sync_markets AND record_price_snapshots, counted on the funnel.
- [x] 1a+. Concentration detector for the NEXT firehose: any series over 25% of
      one fetch is logged with its share.
- [x] 1b. `--purge-markets` maintenance action, dry-run + PURGE-ORPHAN-MARKETS
      token, batched deletes, VACUUM FULL, measured reclaim estimate.
- [x] 1c. DB growth rate (MB/day trailing 7d) + days-to-full in the daily digest.
- [x] Read-only `day7` dispatch so the coverage question is answerable in prod.
- [ ] OPERATOR: dispatch purge dry run, read it, then re-run with the token.
- [ ] Re-check the growth line after the purge — it should go negative once,
      then flatten near zero if the ingest filter is working.

## Expected space reclaimed — estimate, and how it was derived
No per-table byte figures were available (the census output did not come
through in the message), so this is derived from the operator's own numbers and
should be treated as an estimate the dry run will replace with a measurement.

  60 MB/day / 123,000 new rows on 08-15  ~=  490 bytes per market row all-in
  374,000 parlay rows x ~490 bytes       ~=  180 MB

So roughly **150-250 MB**, i.e. about half the database, taking usage from
~386 MB to ~200 MB and headroom from 126 MB to ~310 MB. The plan reports the
exact figure before anything is deleted: it reads the real average `markets`
row width from `pg_total_relation_size` and multiplies by the deletable count.

Caveat worth stating: DELETE alone does not return space to the tier — it marks
pages reusable, which stops growth but leaves `pg_database_size` where it was.
The reclaim above requires the VACUUM FULL that execution runs, and that takes
an ACCESS EXCLUSIVE lock on `markets` for its duration. A five-minute trading
cycle overlapping it will block or lose one tick.

## Day-7: what it needs per category, from here
Thresholds are in `src/execution/day7.py` and none of them changed:

- **24 live recorded hours per category** (MIN_HOURS_TO_PROJECT) before ANY N is
  emitted. Below it the report prints "too little to project from" rather than a
  number, deliberately.
- **200 trade prints per category** (MIN_PRINTS_TO_MEASURE) before the
  multi-level rate is MEASURED. Below it the report carries 0.10 from the probe
  and labels it CARRIED — the probe was taken on liquid markets and is a poor
  guide to weather.
- **200 recognised fills** (TARGET_RECOGNISED_FILLS) before capture is more than
  noise at 1-3 cent spreads. days_to_sample = 200 / (prints_per_hour x 24 x rate).

56h live is pooled; the gate is per category and never pooled. Sports may well
clear 24h while weather does not, and that finding stands on its own — maker
stays off for weather in that case. The `day7` dispatch answers it directly.

## Review
Ingest filter: 14 tests. Purge: 16 tests, TDD. Growth: 8 tests including a
reproduction of the measured emergency (60 MB/day vs 126 MB -> ~2.1 days).
Full suite 874 passed, 2 deselected.

---

# Phase 3 — fill-simulator wiring (spec approved 2026-08-17)

Restated from the operator's approval and written down before implementation,
because everything below was previously carried verbally while the code that
implements it sat unreachable.

## Status of the parts
Built and unit-tested ALREADY: `execution/replay.py` (book reconstruction, four
refusals), `execution/fill_sim.py` (the fill rule), `execution/walkup.py` (the
ladder), `execution/shadow.py` (`simulate_order`, `report_by_category`),
`execution/preflight.py` (the maker blockers), `models/shadow.py`.

MISSING: the wiring. Nothing in the pipeline calls `simulate_order`. Grep for
callers returns only preflight (which reads reports) and the tests. This is the
L27 shape for the fourth time — the unit is right and the path between it and
production has nothing checking it.

## FILL RULE (approved, from the probe findings)
- A resting YES bid at price P fills ONLY when a print has
  `taker_outcome_side == "no"`, `yes_price` strictly LESS than P,
  `is_block_trade == false`, and the reconstructed book confirms the order was
  still resting at that moment.
- Rationale: Kalshi matches strict price-time priority and consumes bids
  best-first, so a taker reaching a price worse than P must have exhausted all
  of level P including us. No queue-position assumption — queue position is
  confirmed absent from public data, so any rule needing it would be invention.
- At-level partials are DISCARDED. Only ~10% of taker events touched >= 2
  levels on the liquid probe; the bias lands in the count, not the price.
- Fills on trade-through, never on touch.
- An order whose rest spans a sequence gap is UNPROVEN: a third state, excluded
  from fill-frequency entirely rather than counted either way.

## REPORTING (approved, with the two-biases ruling)
- Two floors, never collapsed:
  1. recognised-fill spread capture — floor on per-fill economics, compared to
     taker per fill through the instrumented gap.
  2. recognised-fill frequency — floor on volume.
- NO pooled shadow-PnL headline. The fill rule over-represents adverse
  selection by construction (a trade-through means the market moved decisively
  against the resting side), so one PnL number would launder that bias into a
  verdict. A test asserting no combined figure appears already exists — keep it.

## MECHANICS (approved)
- Walk-up ladder from the start price toward cap = `p_model` minus required
  edge. Execution can never trade away the edge that justified the trade. The
  cap-stops-the-walk case is demonstrated by a test, not asserted — exists,
  keep it.
- Decimal end-to-end on the maker path. Fractional contracts are first-class:
  1,541 of 2,299 observed fills were non-integer.
- SHADOW ONLY. ShadowMakerOrder rows in their own table; taker paper fills
  continue untouched so the 50-trade gate keeps exactly one meaning.
  `MAKER_ENABLED` and `SHADOW_MAKER_ENABLED` both stay default-false.
- Every parameter (rest time, step size, cadence) config-driven — Phase 4
  sweep targets, not constants.

## VALIDATION GATING
- N derived PER CATEGORY from measured trade-through rates (`day7.py`).
- 24 live recorded hours AND 200 prints minimum before any N is emitted.
- NEVER pooled: a liquid category must not carry an illiquid one through
  validation. If weather cannot produce a validatable sample that finding
  stands on its own and maker stays off for weather; sports may promote
  independently.
- Build now — the simulator is measurement-independent. The gate applies at
  PROMOTION time, not build time.

## Safety statement (per CLAUDE.md)
- Shadow only. No path here places, sizes or blocks a real order.
- `paper_trading_mode` / `mode` / `can_trade_live` untouched. Both maker flags
  default false and the wiring is a no-op while they are.
- Writes go to `shadow_maker_orders` alone. `trades`, `positions` and the
  50-trade gate counter are never touched, so the gate cannot change meaning.
- Risk limits untouched: quarter-Kelly, 3%/trade, 25% exposure, 20% breaker.
- A simulation failure must never fail a trading cycle — it is reporting.

## Tasks
- [ ] W1. Call `simulate_order` from the execution loop after a taker paper
      trade is placed, gated on SHADOW_MAKER_ENABLED, exception-isolated.
- [ ] W2. Wiring tests in the L27 shape: the pipeline CALLS it when enabled and
      does NOT when disabled, asserted against the real entry point rather than
      by importing the unit.
- [ ] W3. Assert the shadow path never writes to `trades` / `positions` and
      never moves `paper_trade_count`.
- [ ] W4. Two-floor report in the daily digest, per category, no pooled PnL.
- [ ] W5. Evidence recorded here BEFORE SHADOW_MAKER_ENABLED is set anywhere.

---

# SPEC (for approval, NOT built): per-series maker enable

Written per the approved requirements. Nothing below is implemented. Two items
marked **RULING NEEDED** change or extend what was agreed and want an explicit
yes before I build.

## Requirements as given
1. Config expresses a per-series allow-list. Empty = maker nowhere. Empty is
   the default.
2. A series absent from the list is taker, regardless of evidence.
3. The preflight checklist gates the FIRST addition of any series.
4. The digest names which series are maker-enabled, so state is always visible.
5. `TRADING_MAKER_ENABLED` is a global master that overrides the list to OFF.
   Two independent conditions to enable, one to kill.

## Config
    TRADING_MAKER_ENABLED         bool, default False   (global master)
    TRADING_MAKER_ENABLED_SERIES  str,  default ""      (comma-separated)

Matched on the whole series token, case-insensitive, exactly as the ingest
exclusion list is — "KXHIGH" as a prefix rule would enable all seven cities at
once, which is the failure this spec exists to prevent.

**Interaction with L31, and it matters here.** `_env_str` now treats an empty
value as absent and returns the coded default. That is safe ONLY because this
default is itself empty: absent, empty and "maker nowhere" all coincide. If the
default were ever made non-empty, an operator could not turn it off by clearing
the repository variable — they would have to set it to a sentinel. The default
must therefore stay `""` permanently, and a test should assert that.

## Resolution
    def maker_allowed_for(market_id: str) -> bool:
        if not MAKER_ENABLED:            # global master, kills everything
            return False
        return series_of(market_id) in MAKER_ENABLED_SERIES

Two independent conditions to enable, one to kill, exactly as specified.

## Enforcement — the part that carries the real risk
`ORDER_TYPE` is currently a single global string read by BOTH `ev/fills.py`
(`fill_prices`) and `ev/calculator.py` (`calculate_ev`). Per-series maker means
that global becomes a per-market resolution, and both readers must resolve it
the SAME way for the same market.

If evaluation resolves maker while execution resolves taker, the trade is
justified at one price and filled at another. That is trade 1/50 exactly: NO
edge +0.0329 at 91c versus +0.0229 at 92c, either side of the 0.03 gate it
passed on. `src/ev/fills.py` exists because of it.

So the resolved order type must be computed ONCE per market and threaded to
both, never read independently in two places. The test for this is an identity
test in the L26 shape: for a market, the order type used by `calculate_ev` and
the order type used by `_compute_fill_price` must be asserted equal — compared
directly, not each compared to a constant.

Note also that paper trading currently bypasses maker pricing entirely when
`PAPER_CONSERVATIVE_FILLS` is on, which it is. So enabling a series changes
nothing about paper fills until that flag is deliberately changed — a third
condition, and one worth keeping.

## Preflight gating
**RULING NEEDED (1).** As specified, preflight gates the FIRST addition of any
series. I propose something stricter: preflight is re-checked EVERY cycle while
the list is non-empty, and any blocker forces maker off for all series with a
loud error, rather than only being consulted when a series is first added.

Reason: a gate that runs once is a gate that was true once. The decimal
migration, the "capture beats taker" check and the live-gate check can all stop
being true after the addition, and a first-addition-only gate would never look
again. It also fails closed, which is the direction this system takes
everywhere else.

Cost: an extra checklist run per cycle. It is DB-only and cheap.

## Evidence, at the granularity of the switch
**RULING NEEDED (2).** `day7.measure` currently buckets by CLAIMING MODEL, with
per-series print counts as detail only. A per-series switch needs a per-series
bar: hours, prints, own measured multi-level rate, and N — all seven computed
independently, not one model number with a footnote.

So `scope_for_market` needs to return series-level buckets for WeatherModel
(e.g. `WeatherModel:KXHIGHLAX`), and `recorder_health` inherits it through the
same injected scope function it already takes. Otherwise the switch is finer
than the evidence, which is how LA licenses Denver — the exact failure this
whole spec exists to prevent, reappearing between the measurement and the
control.

On current data that likely means LAX/CHI/NY/MIA reach their own bars and
AUS/PHIL/DEN do not, which is the intended outcome and a finding in itself.

## Visibility
Per cycle and in the daily digest, always, including when nothing is enabled:

    maker: OFF (global master disabled)
    maker: OFF (master on, no series enabled)
    maker: ENABLED for KXHIGHLAX, KXHIGHCHI — 5 series taker

Stated when empty for the same reason the ingest exclusion list is: a filter
that reports only when it does something makes "off" and "did nothing this
cycle" identical, which cost a day on 2026-08-17 (L31).

## Safety statement (per CLAUDE.md)
- Defaults are OFF and empty. Doing nothing keeps every series on taker.
- Nothing here touches `mode`, `paper_trading_mode`, `can_trade_live` or the
  50-trade gate. Maker/taker is an execution-style choice within paper.
- Risk limits untouched: quarter-Kelly, 3%/trade, 25% exposure, 20% breaker.
- Fails closed everywhere: unknown series, empty list, master off, preflight
  blocker, or a resolution disagreement between evaluation and execution all
  resolve to taker.
- No path here can place a live order; live remains gated separately.

## Tests to write (TDD, before implementation)
- master off + series listed -> taker. Master on + series absent -> taker.
  Master on + series listed -> maker. Empty list -> taker everywhere.
- default config resolves to maker nowhere, asserted against the shipped
  defaults rather than a fixture.
- the default series list is empty, asserted directly (the L31 interaction).
- whole-token matching: `KXHIGH` does not enable `KXHIGHNY`.
- IDENTITY: evaluation and execution resolve the same order type for the same
  market, compared to each other.
- preflight blocker forces maker off for every series, with the reason named.
- digest names enabled series, and says so when none are.
- per-series day-7 bars computed independently; one series clearing does not
  make another projectable.

## Rulings (all approved 2026-08-18) and build status
1. Preflight CONTINUOUS while the list is non-empty; any blocker forces maker
   off for ALL series with a loud error. APPROVED, BUILT.
2. day-7 re-cut to per-series bars — hours, prints, own rate, own N, each of
   the seven independently. APPROVED, BUILT.
3. `PAPER_CONSERVATIVE_FILLS` stays on as the fourth layer; enabling a series
   is shadow-only until it is separately changed. APPROVED, BUILT — and the
   two enabled states render differently, per the operator's addition that two
   config states meaning different things must never look identical (L31).

## Review — landed 2026-08-18
- `execution/allowlist.py`: `maker_allowed_for` (master AND list),
  `order_type_for`, `resolve_order_type` (adds the continuous preflight gate),
  `describe` (three distinct states).
- Evidence granularity: `scope_for_market` returns `WeatherModel:KXHIGHLAX`
  etc.; `recorder_health` inherits it through the scope function it already
  took, so hours and prints stay one population.
- Single resolution: the scorer resolves once per market and the engine
  resolves once in `execute` and THREADS it into `_compute_fill_price`.
  Pricing no longer resolves anything itself — it was briefly given the engine
  to do so, which both rebuilt the trade 1/50 divergence risk and gave a pure
  pricing helper a database dependency.
- `ORDER_TYPE` default flipped "maker" -> "taker". It is now a fallback only;
  with a per-series list a single global cannot describe any market, so the
  fallback must be the conservative side. Two live-path tests updated: an
  unlisted series now prices at the touch, which is the approved default.
- Visibility every cycle and in the daily digest, including when off.

Tests: 11 allow-list, 8 order-type identity (compared to each other, never to
a constant), 6 continuous-preflight, 7 per-series day-7. A shared
`reload_config` fixture restores module state — the first version leaked a
flipped MAKER_ENABLED into unrelated engine tests, which only showed up in the
full-suite run.

Full suite 1002 passed.

## Still human-gated, unchanged
- Adding any series to `TRADING_MAKER_ENABLED_SERIES`.
- `TRADING_MAKER_ENABLED` itself.
- `PAPER_CONSERVATIVE_FILLS`, the step that makes an enabled series price paper
  fills rather than only shadow.
- Live mode and the 50-trade gate.

---

# Transfer diet (2026-08-23) — production stopped, Neon transfer quota exceeded

Neon closed every connection: cycles, recorder, live-checks all down at once.
A NEW resource axis. Storage had a growth line; transfer had nothing, and every
existing test bounded query COUNT while none bounded BYTES.

## The distinction the whole diagnosis turns on
Rows SCANNED are free — the server does that work. Rows RETURNED cross the
wire. An aggregate over ten million rows costs one row of transfer; a bare
projection over ten million rows costs ten million.

## Biggest consumer: found, and it was mine
`recorder_health` selected `(market_ticker, received_at)` for EVERY row of
`orderbook_delta_raw` to classify each row live/dead in Python — added in the
recorder-liveness commit. `deployment_state` calls it once per cycle, OUTSIDE
the daily-heartbeat block (run_trading.py:382 vs :401).

    650,000 rows x ~60 B  = 39 MB per call
    x 288 cycles/day      = 11.2 GB/day
    x 30 days             = 337 GB/month

That single query is ~93% of all transfer and it grew linearly with the tape,
which is why it detonated now rather than in week one.

## Measured breakdown, per cycle
    BEFORE                                    AFTER
    recorder_health   39.0 MB   (93%)         0.000 MB   pulse: 3 scalars
    sync_markets       1.8 MB   ( 4%)         0.260 MB   hash-check + changed only
    scorer join        0.6 MB   ( 1%)         0.613 MB   unchanged
    sync pre-check     0.2 MB                 (folded above)
    price snapshots    0.1 MB                 0.130 MB   unchanged
    TOTAL/cycle       41.7 MB                 1.041 MB     -97.5%

    12.0 GB/day  ->  0.36 GB/day      362 GB/month  ->  10.8 GB/month

## Fixed
- [x] `recorder_health` aggregates SERVER-SIDE. Result is one row per
      (market, category, hour, liveness) instead of one per message. Liveness
      is decided in SQL against each market's close_date, so the per-row
      semantics are unchanged — an hour straddling a close still splits.
- [x] `recorder_pulse` added for the per-cycle path: count, distinct markets,
      max(received_at). Three scalars. Even the aggregated health query grows
      with HOURS RECORDED, so on a per-cycle path it would creep back toward
      the outage over a month; coverage detail now runs daily and on dispatch.
- [x] `sync_markets` skips unchanged rows via a `content_hash` column. It was
      rewriting title+rules for every market every cycle. The hash covers every
      written field deliberately: Kalshi reworded the settlement clause of all
      seven temperature series on 2026-08-14 and touched nothing else, and a
      comparison that skipped `rules` would have served stale text forever.
      32 bytes fetched to stand in for ~700 bytes written.
- [x] Transfer meter + `📡 Transfer` digest line, warning at 70%. Month-to-date
      GB, MB/day, and projected days of headroom.
- [x] Class-level test: no statement against a growing table may be a bare
      projection. Covers day7 and db_stats too, not just the query that bit.

## RULING NEEDED — free tier is not viable at 5-minute cadence
Post-fix is 10.8 GB/month against a 5 GB free tier. The remaining cost is
almost entirely the cadence, not any one query:

    cycle     GB/month    free tier
     5 min      10.8       OVER
    10 min       6.3       OVER
    15 min       4.8       OK
    30 min       3.3       OK

Options, in the order I would take them:
  A. Cycle every 15 min -> 4.8 GB/month, inside the free tier. Costs scoring
     freshness: a market moving between ticks is seen three ticks later.
  B. Stay at 5 min on the paid plan. ~11 GB/month is small on any paid tier.
  C. Split cadence: score every 5 min, ingest markets every 30. Only 8.9
     GB/month — the scorer join dominates, so this does NOT get under on its
     own. Not sufficient alone.

My recommendation: (B) if the upgrade is happening anyway, since 5-minute
scoring is worth more than the saving; (A) if free tier is a hard constraint.
Not my call — it trades money against scoring freshness.

## UNVERIFIED, and it matters
I have assumed the 5 GB figure and that Neon bills egress only. If ingress
counts too, the recorder's 58 MB/day of writes (1.7 GB/month) is included in
the numbers above; if not, subtract it. `TRADING_TRANSFER_QUOTA_GB` is
configurable for exactly this reason, and the meter will resolve the question
empirically within a few days by comparing our number against the console.

## PROPOSAL, NOT BUILT — orderbook_delta_raw compaction
The tape is 650k+ rows. Note it is a WRITE cost (58 MB/day), not a read cost,
now that nothing reads it per cycle — so retention helps STORAGE, and only
payload slimming helps TRANSFER.

  1. Delta payloads are redundant. `OrderbookDeltaRaw` already denormalises
     market_ticker, sid, seq, side, price_dollars, delta_fp and ts_ms — which
     is the entire content of a delta message. Storing the raw JSON too roughly
     doubles the write. Proposal: store payload=NULL for msg_type='delta',
     keep it for 'trade' and 'snapshot'.
     Saves ~35 MB/day write, ~1 GB/month, and roughly halves tape growth.
     COST: the "always re-derivable from the original message" property is
     given up for deltas specifically. I would want that ruled on explicitly
     rather than assumed.
  2. Trade payloads stay. `fill_sim` needs taker_outcome_side, count_fp and
     is_block_trade, which are not denormalised.
  3. Expiry: deltas and snapshots older than the shadow window (proposed 7
     days) are deletable — replay only ever reconstructs recent books. Trade
     rows stay for the full DELTA_VALIDATION_DAYS=60, since day-7 measures
     trade-through rates over a rolling window.
     Steady state under this: ~7 days of deltas + 60 days of trades, roughly
     150-200k rows instead of unbounded growth.

Wants a ruling before I build it: (1) gives up a stated design property, and
(3) deletes data that cannot be re-collected.

---

# Build-window probes (2026-08-24) — findings, awaiting rulings

Both are READ-ONLY probes run during the Neon outage. No production writes, no
ingest or recorder changes.

## 4. WEATHER EXPANSION — 14 new high-temp series verified, ready to stage

Enumerated via `GET /series?category=Climate and Weather` (354 series). **42
daily CLI-settled temperature series are live**: 21 cities x {high, low}. All
settle on The Weather Company, all name a `CLI***` code, all pass the existing
settlement guard's two checks with no guard change needed.

CLI->station mapping was **confirmed empirically** through IEM's CLI product
database (`json/cli.py?station=<ICAO>`), which returns the AWIPS id and NWS
station name per ICAO — a one-to-one proof rather than an inference. Three
airport traps were caught this way, all of the Chicago/Midway kind:

    Dallas       CLIDFW -> DFW, not Love Field
    Washington   CLIDCA -> National, not Dulles
    Houston      CLIHOU -> HOBBY, not Bush Intercontinental

**Every one of the 14 new stations verified live, nothing assumed:**
- MOS (MEX): `latest_forecast_for` returned a forecast for all 14 ICAOs. Zero
  failures.
- GHCN: `fetch_daily_max` returned data for all 14.
- **Settlement cross-validation**, the strongest evidence: implied settlement
  temperature reconstructed from ~50 settled strike ladders per series and
  compared against GHCN. **13/14 at 100% bracket match.** Controls KXHIGHNY
  43/43 and KXHIGHCHI 55/55 validate the method. Houston tested against IAH
  scored 14/54 versus Hobby's 49/54 — independent confirmation of the mapping.

### RECOMMEND ADDING (12): TSEA TBOS TLV TSFO TDAL TPHX TOKC TDC TSATX TNOLA TATL TMIN
Pure config — a `Station` row each. No code change. Liquidity is comparable to
the existing book (THOU 9232, TSEA 8888, TBOS 7364 against KXHIGHAUS 9343).

### RECOMMEND DEFERRING (2)
- **KXHIGHTHOU (Houston)** — GHCN is not a clean truth series here. Against the
  NWS CLI product for KHOU over Jul 1-Aug 22: 46/51 identical, **5 days
  diverged by 2-4 F**, and Kalshi settled on the CLI value. Every other station
  was 51/51. Fitting sigma on GHCN would calibrate against a series that
  disagrees with settlement ~10% of the time. Needs an IEM CLI truth feed first.
- **KXHIGHTSAN (San Diego)** — launched 2026-08-20, 24 settled rows, 1-day
  volume 38 contracts. Mapping verifies; there is nothing to validate against.

### LOW-TEMP SERIES (21) — code change, not config. NOT recommended yet.
`truth.fetch_daily_max` hardcodes `dataTypes: "TMAX"`, and `mos.parse_run`
deliberately skips 12Z valid times BECAUSE they carry the overnight minimum.
The data exists — MEX 12Z rows confirmed to carry usable minima — but this is a
predictor and truth-source change, and it doubles the cell count. Separate
piece of work, separate ruling.

- [ ] RULING: add the 12? Fits cannot be staged until Neon is back — the refit
      harness writes `weather_cell_fits`. Station rows and the guard-side work
      can land now; fits run post-Sept-1.

## 5. ECONOMICS MODEL — probe done, and I recommend NOT building it

Data plumbing is unambiguously feasible. Validation is not, on any useful
timescale, and that is the finding that should drive the decision.

**Sources verified by live fetch:**
- Cleveland Fed nowcast: three undocumented public JSON endpoints, no key.
  **155 completed (pre-print nowcast, actual) pairs** for headline and core CPI
  MoM and YoY, 2013-07 to 2026-07. Per-target-month daily evolution — a genuine
  vintage archive, better than expected.
- Atlanta Fed GDPNow: Excel workbook, no key. 1,871 daily vintage rows plus a
  **60-quarter scored track record** (MAE 0.773pp, RMSE 1.155pp).
- FRED: API needs a key we do not have; the CSV graph endpoint works without
  one. ALFRED first-print vintages are gated.

**Kalshi contracts** are well-specified and liquid: KXCPI, KXCPIYOY, KXCPICORE,
KXPCECORE, KXFED, KXFEDDECISION, KXGDP, KXU3, KXPAYROLLS. They settle on
exactly the statistics these nowcasts produce. KXGDP settles on the BEA
**advance** estimate, which is precisely what GDPNow's track record scores
against.

**Two clocks, and conflating them is the trap:**
- *Clock 1 — is the forecast skilled vs climatology?* Needs only (predicted
  probability, outcome). Fully backfillable TODAY: ~155 pairs for CPI clears
  N>=60 immediately with room to hold out. GDP has 60 total, so it cannot both
  fit and hold out.
- *Clock 2 — is there edge against the order book?* Cannot be backfilled at
  all. Needs settled markets paired with recorded pre-close books, and Kalshi
  retains only ~2 settled events per series. The corpus starts at zero and
  accrues at the release cadence:

      CPI / core / YoY   12 obs/yr  ->  5 years to N=60
      FOMC                8 obs/yr  ->  7.5 years
      GDP                 4 obs/yr  ->  15 years

  A CPI event's 15-26 strikes are ONE observation, not fifteen — they all
  resolve off one BLS print and form a monotone ladder. Counting strikes as
  pairs would inflate N by 15-25x and hand the gate a meaningless number.

**And the prior on Clock 2 is bad.** The Cleveland Fed nowcast is free, public,
daily, and watched by everyone pricing these contracts; the honest null is that
KXCPI already embeds it. Clock 1 evidence is not evidence of edge — it is
evidence about a number the market can also see. Meanwhile the CPI ladders
quote 5-7 cents wide (KXCPICORE: 24h volume zero across 11 near strikes), and
the deepest instrument, KXFEDDECISION, is quoted 1 cent wide because it tracks
fed funds futures that thousands of desks arbitrage.

- [ ] RULING: I recommend NOT building this. It would be the first model in the
      system that cannot be validated to the standard weather was held to —
      five years minimum before its own gate could honestly pass. If it is
      built anyway it should be explicitly labelled unvalidatable-by-design,
      with a deliberately redesigned gate, rather than quietly inheriting a
      promotion path it cannot reach.
      Also noted: `KXPAYROLLS`'s registered settlement_sources URL points at
      the PPI release, not the Employment Situation. Rules text is correct,
      Kalshi's metadata is wrong — do not drive ingest off that field.

## 6. Phase 4 sweep harness — NOT started
Deferred: 4 and 5 consumed the window, and the sweep needs local snapshot
history that the DB outage makes awkward to assemble honestly.

---

# CLOSED QUESTION — economics model. Ruled 2026-08-24: DO NOT BUILD.

Recorded as closed so a future session does not reopen it on enthusiasm. It
should only be reopened on the reopening condition at the bottom, which is a
FACT about the market, not an argument.

## What was verified (all by live fetch, 2026-08-24)
- Cleveland Fed inflation nowcast: three public JSON endpoints, no API key.
  **155 completed (pre-print nowcast, actual) pairs** for headline and core CPI,
  MoM and YoY, 2013-07 to 2026-07. Real vintage archive.
- Atlanta Fed GDPNow: public Excel workbook, no key. 1,871 daily vintage rows
  and a **60-quarter scored track record** (MAE 0.773pp, RMSE 1.155pp).
- FRED: API needs a key we lack; the CSV graph endpoint works. ALFRED
  first-print vintages are gated.
- Kalshi contracts are real, liquid and well-specified: KXCPI, KXCPIYOY,
  KXCPICORE, KXPCECORE, KXFED, KXFEDDECISION, KXGDP, KXU3, KXPAYROLLS. KXGDP
  settles on the BEA **advance** estimate, which is exactly what GDPNow's track
  record scores against.

**So the plumbing is not the problem. It works.**

## Why it is closed anyway
Two clocks, and conflating them is the trap:

- *Skill vs climatology* is backfillable today. ~155 CPI pairs clears N>=60
  immediately with room to hold out.
- *Edge vs the order book* cannot be backfilled at all. It needs settled markets
  paired with recorded pre-close books, and Kalshi retains ~2 settled events per
  series. The corpus starts at zero and accrues at the release cadence:

      CPI / core / YoY   12 obs/yr  ->   5 years to N=60
      FOMC                8 obs/yr  ->   7.5 years
      GDP                 4 obs/yr  ->  15 years

  A CPI event's 15-26 strikes are ONE observation, not fifteen: they all resolve
  off a single BLS print and form a monotone ladder. Counting strikes as pairs
  would inflate N by 15-25x and hand the gate a number that means nothing.

And the prior is bad independently of the timescale. The Cleveland Fed nowcast
is free, public, daily, and watched by everyone pricing these contracts, so the
honest null is that KXCPI already embeds it. Skill-vs-climatology is not
evidence of edge — it is evidence about a number the market can also see.
Meanwhile the CPI ladders quote 5-7 cents wide (KXCPICORE: 24h volume of zero
across 11 near strikes), and the one deep instrument, KXFEDDECISION, quotes 1
cent wide precisely because it tracks fed funds futures that thousands of desks
arbitrage.

Building it would produce the first model here that cannot be validated to the
standard weather was held to — five years minimum before its own promotion gate
could honestly pass.

## REOPENING CONDITION
Kalshi's economics ladders visibly and persistently diverging from the public
nowcast — i.e. observed evidence that the market is NOT pricing the free input.
That is a measurement, not an opinion, and it is cheap to check: compare the
ladder-implied distribution against the Cleveland Fed nowcast on a few consecutive
prints. Absent that, the answer stays no.

## Incidental finding, worth keeping
`KXPAYROLLS`'s registered `settlement_sources` URL points at the PPI release,
not the Employment Situation. The rules text is correct and Kalshi's metadata is
wrong — never drive an ingest off that field.

---

# PRE-SPEC (not built) — recorder on a liquid-hours schedule

Held for the Sept 1 console numbers, per the ruling. Written now so the decision
is a trade with costs attached rather than a guess.

## The problem
Neon meters compute hours and scales to zero only while nothing is connected.
The recorder holds a connection ~55 min of every hour, so it keeps the database
awake round-the-clock: **~660 h/month against a 100 h budget**. The 5->15 minute
cadence change cut cycle compute from ~216 to ~72 h/month, which is real, but
the recorder alone is ~6.6x over on its own.

## The reframing that makes a cut defensible
The recorder's job is no longer 24/7 archival. It is recent tape for shadow
validation on markets that actually trade — the fill simulator reads a rest
window measured in seconds, and `markets_to_record` already subscribes only to
live markets we hold or are scoring.

## Proposed shape
Run the recorder on US waking hours rather than continuously. 13:00-01:00 UTC
(09:00-21:00 ET) is 12 h/day -> ~360 h/month, still over. 8 h/day -> ~240.
To reach 100-150 h/month the recorder runs roughly **5-6 h/day**, which means
choosing WHICH hours, not merely fewer.

## What that costs, stated as evidence and not as hours
This is the part that makes it a trade:

1. **Shadow orders outside the recorded window become `unproven`, not
   `unfilled`.** The fill rule already has that third state and excludes it from
   fill-frequency entirely, so the effect is a smaller sample rather than a
   biased one — provided the recorded hours are not correlated with fill
   likelihood. They almost certainly ARE correlated (liquid hours fill more), so
   the frequency floor would be measured on the most favourable hours of the day
   and must be labelled as such. That is a real distortion and it points the
   wrong way: it would make maker capture look better than it is.
2. **Day-7 coverage hours stop accruing during the gap**, so the 24-hour
   per-series bar takes proportionally longer in wall-clock time.
3. **Sequence gaps at every start/stop boundary**, which `replay` correctly
   refuses to reconstruct across. One extra unusable boundary per day per
   market.

## The measurement that should decide it
Before cutting hours, measure the hour-of-day distribution of recognised fills
in the shadow record. If fills concentrate in a few hours, the cut is nearly
free and the schedule should track them. If they are uniform, the cut costs
sample proportionally and the honest options are a paid plan or a smaller
subscribe list. That query is cheap and needs only data we already record.

- [ ] Sept 1: db_stats -> console-vs-meter for transfer AND compute -> the
      hour-of-day fill distribution -> then this ruling.

---

# RE-ENTRY 2026-10-06 — diagnosis of the four problems, then the priority-reset plan

Evidence sources: the public GitHub Actions API (run and step outcomes and
annotations; logs need auth and were NOT read), code reading, Neon docs, and
1,149,876 public Kalshi trade prints (19 weather series, Sep 22–Oct 5).

## D0. Finding above all four: the scheduler, not the bot, sets the cadence
GitHub cron has delivered ~5–8 runs/day since Aug 27, against 96 nominal
(trade, */15) and 24 nominal (recorder, hourly). Runs start 4–8 h late (the
retention cron at 04:25 runs 09:00–11:00). Recorder coverage is ~25% of the
day, in random slots. "Recorder silent 4.5 h" = runs at 04:34 and then 11:43:
GitHub's scheduling gap, not an incident.

## D1. Storage
- **Sep 11 → Oct 1: Neon storage cap reached, writes refused, reads fine.**
  Proof by step: in the failed trade runs, `Apply pending schema migrations`
  (which connects and reads) went GREEN and `Run one paper-trading cycle` went
  red. The recorder connected, ran 55 min, then failed. A compute suspension
  refuses the connection itself, so this was the storage cap. The cleanup
  deadlocked exactly as feared: Neon docs say inserts, updates AND deletes
  fail while over the cap. The bot was dark for 20 days.
- **Recovery on Oct 1 between 15:57 and 19:06 UTC** came from Neon raising
  the Free cap from 0.5 GB to 1 GB (changelog dated 2026-10-02: "existing
  projects pick up the new limit automatically"). Nothing we did freed space.
- **Today's red retention (Oct 2–6) is very likely our own exit code, not
  Neon.** `prune.py` returns 1 whenever size ≥ 90% of `TIER_LIMIT_BYTES`,
  which is still hardcoded to 512 MiB. 721 MB = 134% of that constant (the
  heartbeat's number) but 67% of the real 1 GB cap. Supporting evidence: the
  first red day (Sep 11, 09:00) came hours BEFORE writes failed (17:12), i.e.
  the 90% line, and today's run took 49 s, so it did work rather than fail on
  connect. Unconfirmed until the step log is read.
- **Policy bug, which is why steady state never holds:** delta protection is
  anchored, not rolling: `window_end = min(received_at) + 60 d`. The first
  delta is from ~Aug 12, so on ~Oct 11–12 retention deletes EVERY delta before
  Oct 11 in one statement, including the 14-day replay window shadow needs.
  It then re-anchors and protects the next 60 days, which at +49.5 MB/day is
  ~3 GB. A sawtooth that overshoots the cap by 3x and periodically wipes the
  tape. Also: DELETE never shrinks `pg_database_size`; nothing runs VACUUM;
  autovacuum only runs while the compute is awake.
- **Fuse:** at +49.5 MB/day, 721 MB reaches the 1 GB cap around Oct 12–13.
  The anchored delta wipe lands about the same day (first retention run after
  ~Oct 11 22:27 UTC). Whichever comes first decides it. If the cap wins,
  writes AND deletes are refused again and we repeat September; if the wipe
  wins, we lose the tape. Neither is acceptable. Beyond the cap there is no
  graduated overage on Free: writes stop until space is freed or the plan is
  upgraded (Launch: $0.35/GB-month, no fixed fee).

## D2. Live-checks: unrelated
Job conclusion `cancelled` after 15 min, annotation: "The job was not acquired
by Runner of type hosted even after multiple attempts". GitHub infrastructure.
The previous 48 days were green. No action beyond a re-run.

## D3. Compute: hour-of-day distribution
The shadow record has no distribution: 0 recognised fills (see D4, and it
could never have one). The proxy used instead is public trade prints on our 19
series, mid-priced (10–90c) contracts only, which is where a maker rests:

    best 4h  17–21 UTC  37% of mid volume   (uniform would be 17%)
    best 6h  15–21 UTC  53%                 (25%)
    best 8h  14–22 UTC  66%                 (33%)  = 10:00–18:00 ET
    best 12h 12–00 UTC  81%                 (50%)
    trough   07–10 UTC  ~1.2–1.5%/h

The fill opportunity is strongly concentrated, so cutting the recorder is
cheap, provided the hours are chosen and not left to GitHub's random slots.
CAVEAT: "49 h of 100" is OUR estimate (`transfer_meter.compute_estimate`):
distinct UTC hour buckets (a 55-min run straddling :00 counts 2) plus cycles
assumed at 96/day when ~6 run. Neon bills CU-hours, which are wall-hours × CU
size. The console number is the only one that counts, and nobody has read it.

## D4. Shadow: 0 fills on 28 orders is by construction, not evidence
- **Bug A (decisive):** `simulate_shadow_order` runs synchronously at
  placement with `rest_start_ms = now`. It loads trade prints for
  `[now, now+30 s×steps]`, a window in the future, so the tape is always
  empty, the status is always `unfilled`, and nothing ever re-evaluates it.
  This holds at 100% recorder coverage. All 28 rows are void.
- **Bug C:** `start_price_cents = taker_price_cents` (`run_trading.py:229-230`)
  and the walk only goes UP from there. A resting buy at the ask we just paid
  is not a maker order, and `capture = taker − avg ≤ 0` on every possible fill.
  The spec intent was bid+1.
- **Bug B:** the category is never passed (`scorer.py` opp has no `category`), so
  every row is "unknown". That defeats per-series reporting.
- **Bug D:** the recorder's subscribe list is frozen at recorder start.
- Want-of-tape is NOT recognised either: missing coverage is classified
  `unfilled`, never `unproven` (only an `OrderbookGap` row produces unproven).
- Lesson candidate (L27 shape): the wiring test proved the simulator is CALLED,
  not that any outcome other than `unfilled` was reachable.

## Research (cited in chat)
- Kalshi LIP exists (`GET /incentive_programs`, public; 5,477 active).
  **No KXHIGH* programs are active now.** Past weather LIPs were one-off ~$20,
  300-lot target, both sides required: out of reach at a $100 bankroll.
- Fees: `GET /series/{s}` → `fee_type: "quadratic"`, `fee_multiplier: 1` on
  weather (no maker fee). `GET /series/fee_changes` → empty. Our code
  hardcodes 0.07 and reads neither.

---

# PLAN — priority reset (FOR APPROVAL, nothing built)

Safety invariants for every item: paper mode untouched; no risk constant
loosened (P5 adds tests that fail if one is); every DB write path stays in
bounded, committed batches; destructive steps get a dry-run plus a confirm token.

## P0. Storage, before ~Oct 11 — APPROVED 2026-10-06, BUILT
- [ ] P0.0 OPERATOR: dispatch `db_stats` (per-table bytes) and paste the
      retention step log. Still needed: it says whether 14 days of tape fits
      the 500 MB budget (see the review below).
- [x] P0.1 Rolling tape retention, `DELTA_RETENTION_DAYS = 14` from NOW,
      deleted in 10k-row batches (`prune_tape`). The anchored window is gone.
- [x] P0.2 `NEON_CAP_BYTES` (1 GiB) vs `STORAGE_BUDGET_BYTES` (500 MiB) in
      retention.py, the single source of truth. `db_growth.py`'s duplicate
      512 MiB constant was removed. Red = over budget after prune and VACUUM;
      🚨 at 85% of the cap.
- [x] P0.3 `VACUUM (ANALYZE)` on price_snapshots and orderbook_delta_raw
      after every prune (no exclusive lock).
- [x] P0.4 `maintenance vacuum_full` + `VACUUM-FULL-TAPE` token. Dry run
      prints sizes, dead rows and headroom. A space guard refuses any table
      whose worst-case copy would not fit under the cap.
- [x] Tests (see review).

### P0 review — evidence
- RED FIRST: `tests/test_retention_rolling.py` was run against the anchored
  code before any change and 4/5 failed:
    anchored wipe kept 2 of 15 recent days     `assert 2 == (14 + 1)`
    old tape survived                          `[0.0, 15.0, 30.0] == [0.0]`
    sawtooth                                   `assert 300 == 5`
    file kept growing                          `5308416 <= (1687552 * 1.1)`
  All pass after.
- Token gate and verdict through runpy (`tests/test_vacuum_full.py`, 12):
  near-miss and other actions' tokens exit 2; the VACUUM token cannot
  authorise a purge; 600 MB is red at 59% of the cap; 470 MB, which the old
  512 MiB constant called 92%, is green.
- Full suite: 1130 passed.
- Real Postgres 16 (docker, L15): seeded 30 days × 4,000 tape rows (85 MB).
  apply_retention deleted 64,000 in batches; 56,000 = exactly 14 days
  survived. Plain VACUUM did not shrink the file (expected). VACUUM FULL dry
  run → execute reclaimed 41 MB (85 → 44 MB). Then 5 days of steady writes,
  each followed by prune + VACUUM: 46.9, 47.3, 47.4, 47.4, 47.4 MB. Flat.
- OPEN, stated rather than assumed: whether 14 days of tape fits 500 MB
  depends on the tape's bytes/day, which only `db_stats` can show. 14 ×
  49.5 MB/day would be 693 MB, but that 49.5 is the WHOLE database's growth
  under the old policy, not the tape's rate. If db_stats shows the tape alone
  exceeds the budget at 14 days, it needs a ruling: a shorter window, or
  nulling delta payloads sooner. The budget will not be met by deleting
  inside the replay window silently.

## P1. Compute and scheduling — effort M–L, ~1 day
- [ ] P1.0 OPERATOR: read Neon console CU-hours MTD (the real meter).
- [ ] P1.1 Replace hourly recorder crons with ONE "market session" job:
      recorder plus a 15-min trade loop inside a single run, 15–21 UTC (6 h, the
      GitHub-hosted job maximum; 53% of mid volume). Several cron triggers
      (14:45, 15:00, 15:15) under one concurrency group, so a dropped trigger
      does not lose the day. Outside the session: sparse standalone cycles, as
      GitHub delivers them. Fixes Bug D for free (refresh subscriptions inside
      the session).
- [ ] P1.2 Fix the estimator: count wall-minutes of recorder runs, and cycles
      that actually ran. Label it "estimate" until P1.0 calibrates it.
- [ ] Recommend widening to 8 h (14–22, 66%) via two chained jobs only if
      the console shows headroom after one week.

## P2. Shadow repair: makes the Phase 3 evidence real — effort M, ~1 day
- [ ] Defer evaluation: insert `pending`; a resolver resolves rows whose
      window closed; no recorder coverage over the window → `unproven`.
- [ ] Start at the passive side (best bid on our side, or +1 tick), cap
      unchanged (`max_price_cents`). Capture vs the taker price stays the metric.
- [ ] Pass `category` and series; report per series.
- [ ] Mark the 28 existing rows `void` (kept, not deleted, with a reason).
- [ ] Test: an order with a known trade-through inside a recorded window
      resolves `filled` (the L27-shaped test that was missing).

## Main track (a): what is missing to enable maker on ONE weather series, paper
Per-series evidence, never pooled: capture floor (mean capture > 0 with CI)
AND frequency floor (fills/eligible orders ≥ bar), from P2-valid rows only,
inside recorded windows. Missing today: (1) any valid row (P2); (2) coverage
during order windows (P1); (3) a stated N per series before the floors are
read; (4) the paper maker execution path writes taker fills only
(`PAPER_CONSERVATIVE_FILLS`), so an allow-listed series stays shadow-only
until a ruling on that fourth layer. Pick the series by evidence, not now.

## P3. Fee watchdog (d) — effort S, ~2 h
- [ ] Each cycle: `GET /series/{s}` fee_type/fee_multiplier per traded series
      plus `GET /series/fee_changes`. Compare against what `ev/calculator.py`
      models (quadratic, 0.07, no maker fee). Mismatch → refuse that series
      (fail safe) + Telegram, and recompute after-fee edge from the live
      multiplier. API failure → skip trading the series, never assume.

## P4. Kill switches (c) — effort M, ~1 day
Existing (`risk/limits.py`): 3% single trade, 25% exposure, daily-loss
pause, 20% drawdown breaker, cluster cap (weather keyed by series+date).
All are per-trade REFUSALS recomputed each cycle. Missing, genuinely:
- [ ] A latched halt: once tripped, stays tripped until a human clears it
      (DB flag + Telegram). Today the drawdown breaker un-trips on a bounce.
- [ ] Anomaly halts: fill price > X c from the model or quote; bankroll step
      drop > Y% in one cycle; N consecutive Kalshi API errors; stale model
      inputs. No "regime guard" exists in the code; this is the honest version.
- [ ] Silence is not health: 26 consecutive red retention days produced no
      action. The heartbeat lists any workflow red more than 1 day.
- [ ] A demonstrated-failure test per switch (trip, latched, blocks the next
      trade, restart only via an explicit clear).

## P5. Size ramp (e) — effort S–M, before any live flip only
- [ ] Live stage table: 1 contract for the first 20 live trades; step up only
      if realized-vs-paper slippage ≤ stated bar and no halt fired. The ramp
      can only reduce size under the hard caps, never raise them. A test
      asserts each cap is unchanged.

## P6. LIP tracker (b) — effort S, ~2 h, lowest priority
- [ ] Daily: active `/incentive_programs` joined on our series tickers;
      digest line. A separate rewards PnL line only once we can qualify
      (both-sides target size is beyond the current bankroll).

## Scope (f): frozen
No new model types; economics stays closed; low-temp series not started.

## Recommended order
P0 (deadline ~Oct 11) → P1 (compute deadline; P1.0 first) → P2 → P3 → P4 →
P6; P5 at the live-flip ruling. Gate 32/50 means P3+P4+P5 must land before 50.

## Rulings 2026-10-06 (second pass), after the Neon console actuals
Console since Sep 30: compute 7.69 CU-h, storage 761.96 MB, history 13.65 MB,
transfer 44.86 MB, autoscale 0.25<->2 CU.
- [x] Compute line recalibrated to CU-hours (`f3a0276`); the old figure was
      ~6x high. October paces to ~35 CU-h of 100. P1 is DEMOTED to an
      improvement; storage is the only clock.
- [x] Storage reported in decimal MB, as the console does; cap = 10^9 bytes
      (`ce577cb`).
- P1 approved as recommended. Open for confirmation: self-chaining via
  workflow_dispatch, zero off-session cycles, heartbeat once per UTC date.
- Shadow: the 28 rows are to be WIPED (not voided). Rebuild with a failing
  test per defect, shown red before green. Fee read from the API, plus an
  alert on change.
- Liquidity rewards: CLOSED. Reopening condition: a new KXHIGH* program in
  GET /incentive_programs, rechecked weekly in the digest.

## P1 — session job (rulings 2026-10-06 stand), BUILD PLAN
- [ ] `src/session.py`: one job, 15:00–21:00 UTC. A recorder subprocess,
      restarted hourly (fresh subscribe list fixes Bug D), plus
      `python -m src.run_trading` every 15 min, each cycle a fresh process
      exactly as today. Pure `plan_link(now, started)` decides wait / run /
      chain / exit, and is unit-tested.
- [ ] `session.yml`: five cron triggers 13:00–15:30 under one concurrency
      group. Each link stops ≤ 5h40m after it started (the hosted-job limit is
      6h) and, if the window is not done, dispatches its successor via
      workflow_dispatch (GITHUB_TOKEN may trigger workflow_dispatch). A
      duplicate trigger queued behind a finished session sees the window
      closed and exits.
- [ ] `session-watchdog.yml`: crons at 16:05/16:35/17:05. Telegram alert if
      no session run is in progress or has run today (GitHub API, no Neon
      wake). The same job re-enables every workflow, so the 60-day inactivity
      rule never fires (daily, a superset of the monthly ruling; idempotent).
- [ ] trade.yml / book-recorder.yml: schedules REMOVED (zero off-session
      cycles); workflow_dispatch kept.
- [ ] Heartbeat: due once per UTC calendar date, not 24h after the last.
- [ ] Cycle lock: `pg_try_advisory_lock` around the cycle. A second cycle (a
      manual dispatch during a session) exits without touching the DB.
- Safety: no risk constant, sizing, mode or live-gate code touched; each
      cycle is the same `run_trading` process with the same env. Over-exposure:
      unchanged per-trade limits, and fewer cycles, not more. Accidental live:
      `mode` is untouched (DB-only, default paper). DB corruption: the new lock
      makes concurrent cycles impossible, where before only a GitHub
      concurrency group stood between them.
- [x] P1 BUILT (7d22788, b0c2087, 58f646f): session job, successor chaining,
      watchdog + daily re-enable, zero off-session cycles, heartbeat per UTC
      date, advisory cycle lock. 1187 tests; wiring smoke-tested with echo
      subprocesses. FINDING: the 6 h window cannot fit one hosted job
      (5h40m usable), so every day is two links: 15:00–20:40, then 20:40–21:00.

## D. SHADOW REBUILD — PLAN (rulings 2026-10-06; written before building)
Each defect gets a test shown FAILING on the current code before the fix.
1. Tape after placement. Placement writes `pending` with the plan's prices.
   A resolver at the start of each cycle resolves rows whose window has
   closed: simulate if COVERED, otherwise keep pending; `unproven` only
   after 2 h (recorder rows land in batches).
   COVERED = this market was snapshotted at T0 ≤ window start; there was NO
   re-snapshot (a reconnect or a new segment) between T0 and the window end;
   the same connection delivered a row after the window end (any market,
   before this market's next snapshot); and no gap per item 4.
2. Bid+1. Start at our side's best bid + 1¢ (yes_bid, or 100 − yes_ask for
   no). Never at or above the taker price: the ladder is capped at taker − 1¢
   as well as by the model cap, and a 1¢ spread joins the bid. Prices come
   from the opp's quote at decision time.
3. Category and series. The scorer puts the market's category in the opp;
   reports go per SERIES (series_of(market_id)), never pooled.
4. Gap spanning = unproven. Today's gap check only sees gaps tagged with the
   order's own ticker, inside the window. But seq is per SUBSCRIPTION, shared
   by every market in it, and a gap is detected at the NEXT message, which
   can come after the window. New rule: any gap on the connection, detected
   between window start and the first row after window end, makes the order
   unproven.
5. Wipe: maintenance `wipe_void_shadow` + token, dry run first, deletes only
   rows created before the rebuild deploy.
6. Fees (the fee watchdog, early): a FeeSchedule per series from
   GET /series/{s} (`fee_type`, `fee_multiplier`), cached per cycle, last seen
   persisted. Taker = 0.07 × multiplier; maker = 0.0175 × multiplier ONLY for
   `quadratic_with_maker_fees`, else 0. Unknown fee_type → refuse that series
   + alert. Change vs last seen → alert. Replaces the hardcoded 0.07 in
   ev/calculator.py and trading/fees.py. The shadow's maker_fee is now the
   maker rate: it currently charges the TAKER fee to the maker side, which
   understates capture on every row.
   OPEN, needs a ruling (money path): if the fetch FAILS, use a last-seen
   schedule up to 24 h old, else refuse the series.
- Safety: no limit, Kelly, mode or gate code touched. Today's fee numbers are
  unchanged (quadratic × 1 = 0.07), and a test pins that equality, so the EV
  path cannot drift. The shadow path still writes only shadow_maker_orders
  (existing test W3 kept).

## 2026-10-06 (third pass) — evidence
- Q1: Neon lists "Instant restore history | 6 hours, capped at 1 GB of change
  history" as its own line item beside "Postgres storage | 1 GB per project",
  and says the limit "applies to Postgres storage". Separate, though not
  stated as excluded in so many words.
- Q2 calibration (Postgres 16 WAL, forced checkpoint = worst case): DELETE
  630 B/row vs Neon's observed ~55 B/row (~0.087x); shrink 1,642 B per moved
  row -> ~143 B expected in Neon. Full shrink ~455k moved rows -> ~65 MB of
  history expected, ≤ 750 MB worst case.
- [x] Q3 max_chunks + own before/after sizes (ecd5a02).
- [x] A: 2-day delta compaction, 20k batches, 90% stop (3f11dd2); replay
      reads columns (73c9f95). Red first; mutation-proven invariants.
- [x] C: cap defense (d64cbfa). Refinement: cuts only while LIVE ≥ 75%.
- [x] D fees (dca8942) + shadow rebuild (49f8cc0). 1240 tests.
- [ ] INCIDENT: no session run fired on day 1 (L32). Dispatch by hand; decide
      on a backstop and an external trigger.
- [ ] OPERATOR: 2-chunk shrink test; wipe_void_shadow dry run, then token.
- [ ] E: after 3 full session days, rerun the steady-state table.
- FINDING (not fixed): replay checks seq per MARKET, but seq is per
  SUBSCRIPTION, so on real multi-market tape it refuses almost at once. It has
  no production caller; fix before anything depends on replay.
- FINDING (inert today): opp["net_ev"] is the YES side's EV on every call;
  execute_qualifying builds no_ev = -net_ev. Risk/Kelly do not read it, so
  sizing is unaffected; the fee check now uses traded_net_ev.

## Backstop and dead-man's switch (rulings 2026-10-06, fourth pass)
- [x] trade.yml */15 BACKSTOP restored (L32). Overlap is safe: the advisory
      cycle lock makes the second cycle skip; backstop cycles never write
      tape, so shrink_tape's "recorder is writing" check is unaffected.
- [ ] EXIT CRITERION: remove the backstop schedule, in its OWN commit, only
      after 7 CONSECUTIVE days on which market-session ran green AND
      session-watchdog ran green. Evidence: the digest's "🔁 Cycles 24h"
      line showing session N/24 with no ⚠️, for those 7 days.
- [x] cycle_runs table + digest line "Cycles 24h: session N/24 expected,
      backstop M", ⚠️ when the session is short.
- [x] Dead-man ping after each successful cycle (DEADMAN_PING_URL secret).
      OPERATOR: create the healthchecks.io check and add the secret.
- [ ] Measure: GitHub-delivered backstop cycles per day for 7 days (digest).
- [ ] Evaluate before ANY external trigger token: GitHub Environment
      `destructive` with a required reviewer on maintenance's confirm path.
- [ ] E: steady-state table after 3 full session days.

## 2026-10-07
- [x] A: lead gate counted UTC days; settlement is local. The 23:58 UTC
      manual cycle scored after 00:00 UTC and refused all 76 thresholds
      (Oct 6 + Oct 7 ladders). Fixed: station-local date. Not a regression.
      Ladders open 14:00 UTC on D-1 and close 05:00 UTC on D+1, so a lead-1
      ladder exists from 14:00 UTC to local midnight; the 15-21 session sits
      inside it.
- [ ] B RULING NEEDED: the capped /markets walk is 3000/3000 excluded parlays
      (KXMVECROSSCATEGORY-R), so SportsOdds is reached by nothing. With
      mve_filter=exclude the same 3000 slots are single markets (54
      sports-looking series). Odds quota (500/month) vs ~30 cycles/day must
      be budgeted before turning it on.
- [x] C: maintenance pending_matches (read-only).
- [x] Session kick from the backstop (session triggers fired 0/10 on days 1-2).
- [ ] Environments after the shrink test. Dead-man: OPERATOR creates the
      healthchecks.io check and the DEADMAN_PING_URL secret.

## Oracle migration — SPEC FOR APPROVAL: docs/oracle-migration-spec.md
- [ ] Operator: create the VM per §2 (or §3 fallback); report shape/AD obtained.
- [ ] Rulings §7: shape + 24/7 cadence on VM; recorder window; PAYG (not now).
- [ ] Phases B–D only after approval. Cutover by overlap (L32), single writer.

## KILL SWITCHES (9b, ruled 2026-10-09) — PLAN, written before building
A LATCHED halt: once tripped it stays on until a human clears it with a typed
token. Halts refuse NEW trades only; settlement keeps running (the settler
runs before scoring), so open positions still settle.

Store: `halt_events` (switch, detail, tripped_at, cleared_at, cleared_by).
Active = any row with cleared_at NULL. A Telegram alert on every trip; a
`🛑 HALTED` line leads the digest while any halt is active.

Switches (thresholds hardcoded in src/risk/halts.py and pinned by a test):
1. drawdown_latch: equity ≥ 20% below peak. Today's breaker un-trips on a
   bounce; this one does not.
2. bankroll_drop: equity ≥ 10% below its 24 h high (equity recorded per
   cycle in cycle_runs).
3. implausible_edge (pre-trade): |p_model - fill price| ≥ 40 points on an
   opportunity about to execute. That is a broken model, not a bargain. The
   trade is refused and the halt trips.
4. fill_slippage (post-fill): |fill - evaluated| ≥ 3c (live fills; paper
   already refuses any divergence).
5. repeated_errors: the last 3 cycles all failed (cycle_runs ok=False).
6. stale_inputs: newest price snapshot > 60 min old when execution starts
   (ingest silently produced nothing).

Clear: `python -m src.maintenance --clear-halt --confirm CLEAR-HALT` (dry run
lists active halts). maintenance.yml's inputs are consolidated into one
`action` choice so the clear is reachable from Actions before migration.

Safety:
- Over-exposure: halts can only REFUSE trades. They never place or close
  orders and never touch limits, Kelly, mode or the live gate.
- Accidental live: no code path reads or writes `mode`.
- DB integrity: one INSERT per trip, one UPDATE per clear, each in its own
  committed transaction.

Each switch gets a test that trips it, a test that it stays latched after
the condition clears, a test that it blocks the next trade, and a test that
only the token clears it.
