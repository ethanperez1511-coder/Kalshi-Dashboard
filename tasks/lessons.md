# Lessons

Rules distilled from corrections and from bugs that got past a green test suite.
Reviewed at the start of every session.

---

## L1 — A green suite is not evidence. Run the thing against real data.

Phase 1.3 shipped an entity-comparison layer with 30 passing tests, including tests that
specifically covered "different named entity". Then it was run against 2,055 real Kalshi
markets and 1,500 live Polymarket markets and immediately matched
`Alexandru Rafila` to `Alexandru Nazare` — two different candidates for the same office —
because the tests only ever exercised entities that shared *nothing*, never entities that
shared a first name. Accented names ("Cătălin") were being mangled by an ASCII-only regex that
no test noticed either.

**Rule:** for any matching, parsing, or classification logic, run it over the real corpus and
diff old vs new *before* declaring it done. Report the counts (17 -> 13 priced, 4 stopped) and
eyeball the examples. Unit tests encode the failures you already thought of.

## L2 — Test fixtures must be as long/messy as production data.

The first Polymarket tests used short titles ("Will CPI be above 3%?"). Those score 0.6
similarity and get rejected by the fuzzy gate before the entity check ever runs — so the tests
passed for the wrong reason and did not reproduce the bug. Realistic phrasing scores 0.818 and
does reproduce it.

**Rule:** when a bug depends on a threshold, build the fixture that actually crosses the
threshold, and assert the pre-condition (`similarity > 0.7`) inside the test so it cannot
silently stop reproducing.

## L3 — Ask what the deployment does to the code, not just what the code says.

The odds cache was a module-level dict with a 60-minute TTL. Correct for a long-lived process,
completely inert once the bot moved to a GitHub Actions cron — every 5-minute tick is a fresh
interpreter, so the cache was always empty and the TTL never applied. ~864 requests/day against
a ~500/month cap. Nothing in the code looked wrong; the *host* had changed underneath it.

**Rule:** after any hosting/runtime change, re-audit every piece of state that lives in process
memory — caches, counters, rate limiters, "once per day" flags. If it must outlive a cycle, it
belongs in the database.

## L4 — Fixing a fee bug means auditing the whole ledger, not just the fee.

The brief was "live settlement PnL is fee-blind". Fixing it surfaced a second bug in the same
accounting: the live path debited the entry cost at fill and then credited `realized_pnl` —
which already contains the entry cost — at settlement. Double subtraction, hidden because
`sync_live_bankroll` overwrites the bankroll from Kalshi every cycle.

**Rule:** when correcting one term in a money calculation, walk the entire cash path (fill ->
hold -> settle) and check it sums to reality. A sync that overwrites state will mask arithmetic
errors indefinitely.

## L5 — Bug fixes default ON; new capability defaults OFF.

"All new features behind flags, default OFF" is right for capability and wrong for corrections.
A flag on a fee-accounting fix means the default path keeps computing known-wrong PnL — and
that number feeds Kelly sizing.

**Rule:** if the change only makes results more conservative or rejects more inputs, ship it ON
and say so. If it adds a new data source or new behaviour, flag it OFF. State which is which in
the report.

## L6 — Probe an external API before building against it.

ESPN was assumed to be a straightforward free odds source. Probing the live endpoints first
found three things no amount of code-reading would have: the undated endpoint returns
*yesterday's* slate, the `odds` key is dropped entirely (not emptied) once a game is FINAL, and
an Akamai bot manager 403s browser-like and custom User-Agents while passing library defaults.
Each is now locked by a test.

**Rule:** fetch the real payload before writing the parser, and turn every surprising constraint
into a test — especially the ones a future "improvement" would break, like adding a polite
custom User-Agent.

## L7 — Note the side effects of verification steps.

Booting the FastAPI app as a smoke test ran `ensure_schema()` against the real local
`kalshi.db`. Additive and safe, and it had been rehearsed on a copy first — but it was a write
to a production file that was not the stated purpose of the command.

**Rule:** rehearse migrations on a copy, and when a verification step mutates real state, say so
and prove integrity afterwards (row counts before/after, `PRAGMA integrity_check`).

## L8 — "Same entity" is not "same contract". Compare resolution criteria.

Phase 1 shipped an entity check that matched on identity, and the Phase 1 report
called the 13 surviving Polymarket pairs "correct". Phase 1.5 read the actual
contract text and found every one of them mismatched on resolution *window*:
Polymarket resolves "following the 2026 parliamentary election ... 'Other' if no
PM is sworn in by December 31, 2027", Kalshi has no election scoping and closes
2045. Same person, same office, different question — and the bias only ever
points one way, so it reads as durable edge.

**Rule:** for any cross-venue price comparison, the match must clear the
*resolution criteria*, not just the subject: deadline, scoping clause, what
counts as the event, what happens if the event never occurs, and who adjudicates.
Two contracts that can resolve to opposite outcomes are not the same market
however identical their titles.

## L9 — Validate a new gate's threshold against the real distribution before trusting it.

The horizon check produced a constant 6,576-day gap across all 13 pairs. A
constant across every sample usually means the metric is measuring a convention,
not a signal — so the honest next step was to go read the contract text rather
than ship the threshold. It turned out the gap was real, but the reasoning had
to come from evidence, not from the number looking decisive.

**Rule:** measure a new threshold's effect on the real corpus before choosing a
default, and treat a suspiciously uniform result as a reason to verify the
metric, in whichever direction the verification lands.

## L10 — Metadata that is populated by import is metadata that can be empty.

`python -m src.migrate` would have inspected a near-empty `Base.metadata`,
reported "schema up to date", migrated nothing, and left the pipeline to crash
on the column it was run to add. Table registration is a side effect of
importing model modules, and the CLI imported almost none of them. Separately,
`inspect(engine)` caches reflection, so a verification run after a migration in
the same process reported the pre-migration shape.

**Rule:** anything that reflects or enumerates state — schema metadata, plugin
registries, model lists — needs an explicit load step and a test that asserts
the enumeration is complete. Never let correctness depend on which modules
happened to be imported first.

## L11 — Smoke-test the write path, not just the read path.

The forecast archive passed its unit tests and then wrote **zero** MOS rows on
its first live run: MEX publishes twice daily and the code only asked for the 12Z
run, which had not posted yet at that hour. Gridpoint wrote 56 rows in the same
call, so the failure looked like a healthy run with a quiet source.

**Rule:** for anything that accumulates history, run it for real once and assert
the rows landed. Time-of-day and publication-schedule assumptions do not show up
in a mocked test, and an archive that silently records nothing is indistinguishable
from one that had nothing to record — until months later when the history is
needed and is not there.

## L12 — Diagnose the shape of a failure before proposing a fix.

One cell failed its reliability bar. The tempting move is to widen the bar or
refit and re-run. Instead: check whether degradation is uniform (model-level) or
patterned (data-level). It ordered exactly by station seasonality, and the sign
of (fitted sigma - realized sigma) predicted the direction of every miss in both
directions. That made it a stale-fit story, which has a specific remedy — a
rolling window — rather than a vague one.

**Rule:** when a gate fails, first establish whether the failure is uniform or
structured. A structured failure names its own fix; a uniform one means the model
is wrong. Never move the bar to make the failure go away, and never adopt a
remedy without re-running the unchanged gate on fresh held-out data.
