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

---

# PRINCIPLE: A check that cannot fail is not a check.

This is the named rule, not another entry. Every guard, test and monitor is
evaluated against it.

**A check must have a demonstrated failure mode.** Not a plausible one — a
shown one. Break the thing it protects and watch the check go red. If nobody
has ever seen it fail, what exists is the appearance of coverage, and the
appearance is worse than nothing because it stops anyone looking.

Four instances of this, all caught late, all in this codebase:

1. **The self-verifying signature test.** `test_signature_verifies` signed a
   message and verified it with the same padding and message shape the signer
   used. It passed for months while every authenticated Kalshi endpoint
   returned 401. *Any* scheme would have passed it — it compared the code to
   itself, never to the counterparty. Only a live call found it, and only
   because the live flip would have failed on the first request.

2. **The guard that skipped without credentials.** The station-mapping check
   used `skipif(no credentials)`. On every machine without secrets it reported
   success while verifying nothing — precisely the case it existed to catch.

3. **The archive that succeeded with zero rows.** The forecast archive passed
   its unit tests, then wrote zero MOS rows on its first live run because the
   12Z model run had not posted yet. Gridpoint wrote 56 in the same call, so it
   looked like a healthy run against a quiet source.

4. **The migration that migrated nothing.** `python -m src.migrate` would have
   inspected a near-empty `Base.metadata`, reported "schema up to date", and
   changed nothing on Neon — because table registration is a side effect of
   importing model modules and the CLI imported almost none of them.

**How to apply it, before writing the check:**

- Name the failure it catches, then *cause* that failure and watch it go red.
  If you cannot cause it, the check does not test what you think it does.
- Never verify a component against itself. A signature test must verify against
  the counterparty's expectation; a parser test must use a real captured
  payload, not one written from the same assumption as the parser.
- Treat "passed" and "did not run" as different outcomes. A skip, an empty
  result set, and a zero-row write are all *absence of evidence* and must be
  reported as such, never as success.
- For anything that accumulates or writes, assert on the artefact — rows
  landed, bytes written, count non-zero — not on the absence of an exception.
- Prefer checks whose failure is loud by construction: a stale fit that makes
  cells unpriceable, a recorder that exits non-zero when it wrote nothing.

## L13 — "Committed" and "deployed" are different outcomes.

Thirteen commits sat on a local branch for a full working session while I
reported each one as shipped. Production ran July code the whole time: broken
RSA auth, fee-blind settlement, the old Polymarket matcher with the horizon bug,
no weather, no recorder. Every "verified live" claim was true of my laptop and
false of the system that trades.

It is a direct instance of the pinned principle. Nothing checked that the thing
described as deployed was deployed, so the gap could not fail — it could only be
noticed, and only by someone looking at the Actions tab.

**Rule:** a change is not shipped until it is on origin AND observed running.
After any commit that adds a workflow, a migration, or a scheduled job, verify
against the remote rather than the working tree:

    git log origin/main..HEAD --oneline        # must be empty
    git ls-tree --name-only origin/main .github/workflows/

And the standing guard: the daily digest reports the DEPLOYED commit hash from
GITHUB_SHA, so local-versus-production drift is visible in the same message that
claims the system is healthy, instead of being assumed.

## L14 — An unset secret is not an absent value; it is an empty one.

All four workflows failed identically on `Could not parse SQLAlchemy URL from
given URL string`. The obvious reading — a step missing its env block — was
wrong: every step had one. The real mechanism is that an unset GitHub secret
still sets the environment variable, to the empty string, and an empty env var
OVERRIDES a library default rather than falling back to it. `DATABASE_URL` was
therefore `""`, not "unset", and pydantic's default never applied.

The traceback named the library, not the variable, so it read as a code bug for
as long as nobody looked at the secret.

**Rule:** for every externally-supplied configuration value, distinguish three
states — absent, present-but-empty, and present-and-valid — and make the middle
one fail with a message that names the variable and the likely cause. Then check
the derived condition too: a production job that quietly fell back to the local
SQLite default would have gone green while writing to a container filesystem
that is deleted when the job ends, reporting success and persisting nothing.
Falling back is not always safer than crashing.

## L15 — SQLite-lenient is not Postgres-strict. Test against the real engine.

`markets.title` was VARCHAR(500). SQLite ignores VARCHAR lengths entirely, so
months of local runs and 600+ green tests could not catch it. The first real
Postgres insert failed on a multi-leg parlay title measured at 1,381 characters.

Auditing the class rather than the instance found two more that had never
executed on Postgres: `shadow_maker_orders.rest_start_ms` was Integer while
holding an epoch-ms value of ~1.79e12, which overflows int4; and a boolean
column carried `server_default="0"`, which Postgres rejects in favour of a
boolean literal.

**Rule:** for every column, ask which engine enforces the constraint. Anything
SQLite ignores — string length, integer width, type coercion, boolean literals —
is untested by a local suite however green it is. Either test against Postgres,
or assert the schema's *shape* in tests that run anywhere: no length limit on
columns fed by unbounded API text, BigInteger on anything holding epoch
milliseconds, boolean literals in boolean defaults. Measure the real data before
choosing a bound — the title limit was not a close call, it was 2.8x over.

Corollary, from the same session: a test that computes an age from the wall
clock while the code under test uses an injected clock will pass until the date
rolls over. Pin fixtures to the injected clock.

## L16 — Row-at-a-time is free locally and fatal over a network.

The first production cycle hit the 8-minute job cap. The code was correct and
every test was green; the difference was that SQLite makes a query a function
call and Neon makes it a network round-trip. The scorer opened a session PER
MARKET to fetch that market's latest snapshot — ~135,000 sequential round-trips
before scoring began. Ingest added ~15,000 more.

Two things were only visible from production: the round-trip cost, and the fact
that price-derived models were being executed — each querying the database per
market — with their output then discarded by a gate. Work whose result is thrown
away is invisible until it is the thing consuming the budget.

**Rule:** any loop that touches the database once per item is a latent timeout.
Load in one query keyed by the loop variable, write with bulk statements, and
push filters into SQL so the loop walks fewer rows. Assert the statement COUNT
in a test — `before_cursor_execute` makes it cheap — because a count that scales
with input size is the bug, and it fails identically on any engine. And check
what work is discarded downstream: the cheapest optimisation is not doing it.

## L17 — A retry that can restart from the top is an infinite loop wearing a retry's clothes.

Polymarket's Gamma API answers 422 once pagination runs past a few thousand
rows. `raise_for_status()` turned that into an exception, the exception escaped
`get_markets` before the cache was set, and the caller caught it and moved to the
next market — which called `get_markets` again, restarting the walk from offset
0. Every market re-walked, hit the same wall, and the cycle died on the platform
timeout with no bounded loop anywhere in the traceback.

Two rules fall out:

**Never retry a deterministic 4xx.** 4xx means the request is wrong. An
identical request will fail identically, so a retry is guaranteed waste and an
unbounded retry is a hang. 429 and 5xx are the retryable ones; everything else
in the 4xx range should stop.

**Cache the failure, not just the success.** Leaving the cache unset on error is
what converted one failed call into N failed walks. A partial or empty result
that is remembered costs one failure; a result that is not remembered costs one
failure per caller.

And the structural fix, which is worth more than either: **the platform timeout
must be the last resort, not the mechanism.** Give each stage its own budget so
it stops taking new work and returns what it has. Ingest is idempotent and the
next cycle is minutes away, so a partial ingest is worth far more than a
cancelled tick — twice now, one bad stage has destroyed scoring, settlement and
the digest along with it.
