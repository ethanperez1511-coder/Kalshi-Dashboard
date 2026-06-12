CLAUDE.md — Kalshi Core Trading System

Hard Constraints — never violate, no exceptions

These are invariants, not preferences. If a task appears to require breaking one, stop and surface the conflict instead of proceeding. If you're unsure whether a change touches one of these, assume it does.


Paper trading is the default and stays the default. paper_trading_mode must never resolve to False as a side effect of any change. Live trading is enabled only through explicit, deliberate, multi-step environment-variable verification performed by a human. Whenever you touch a code path that reads this flag, prove in your summary that default-true behavior is preserved.
Why: an accidental live flip risks real capital with no human in the loop — the single worst failure mode in this system.
Risk limits are hardcoded and protected — quarter-Kelly sizing, 3% max per trade, 25% max total exposure, 20% drawdown circuit breaker. These are enforced ceilings/floors in code, not configurable conveniences. Any change to the risk layer must assert they still hold — ideally via a test that fails if a limit is loosened.
Why: these bounds are the difference between a drawdown and a blowup.
No mock or fallback data in production paths. If a price or market feed fails, handle the exception natively and fail safe. Never substitute synthetic data to "keep things running."
Why: trading on fabricated prices is worse than not trading at all.
Database integrity. All SQLite writes use proper state locking. Execution states (PENDING, FILLED, TIMEOUT) are strictly typed and validated with Pydantic at the FastAPI boundary. Never leave a transaction in a partial state.



How to work

Plan before non-trivial work. For anything 3+ steps, or any architectural / pipeline / risk change, write a short plan to tasks/todo.md with checkable items and check in before implementing. For pipeline or risk-management changes, the plan must explicitly state how it avoids over-exposure, accidental live execution, and database corruption. If work goes sideways mid-task, stop and re-plan rather than pushing forward.

Use subagents to protect the main context. Offload research and parallel exploration — Kalshi API docs, external sportsbook API shapes, anything investigative — to focused, single-task subagents. Throw more compute at hard problems this way.

Prove it works before calling it done. No task is complete without evidence: mock-data runs, backtest logs, or a passing test.


Every model change (SportsOdds, Finance, Consensus) is tested against simulated market states; probability outputs must land in [0, 1] and within expected bounds.
Every backend change runs the FastAPI suite, including the case where the EV filter / execution engine sees a dropped connection.
Diff your behavior against main when relevant.
Give extra scrutiny to model-calculation edge cases (Kelly fractions, edge minimums) and API rate limits — these are where mistakes hide.


The bar: would a quant developer or staff engineer sign off on this?

Fix bugs autonomously. Given a bug report or a failing test/CI run, find the root cause and fix it — point at the logs, the FastAPI error, the SQLite constraint, or the failing UI test, then resolve it. No hand-holding, no temporary patches.

Prefer the elegant solution for non-trivial changes. Pause and ask whether there's a cleaner approach; if a fix feels hacky, redo it properly. Skip this for obvious one-liners and minor Vite frontend tweaks — don't over-engineer. Challenge your own math and algorithms before presenting them.

Keep changes minimal and decoupled. Touch only what's necessary. Keep model logic separate from EV filtering, and EV filtering separate from execution. Always find root causes; no band-aids.


After the work


Mark tasks/todo.md items complete as you go, and add a short review section with the backtest / mock results that prove the change.
After any correction from me, append the pattern to tasks/lessons.md as a rule that prevents the repeat. Review tasks/lessons.md at the start of each session.
