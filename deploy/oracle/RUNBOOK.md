# Operating the system after cutover

What changes for you once the VM is canonical.

## 1. What is retired

| GitHub workflow | Replaced by on the VM | When |
|---|---|---|
| `trade.yml` (backstop) | `kalshi-cycle.timer` (every 15 min, 24/7) | schedule disabled at cutover |
| `session.yml`, `session-watchdog.yml` | unnecessary: the VM's timer is exact; healthchecks.io watches it | disabled at cutover |
| `book-recorder.yml` | `kalshi-recorder.service` | disabled at cutover |
| `retention.yml` | `kalshi-retention.timer` (daily) | disabled at cutover |
| `weather-refit.yml` | `kalshi-refit.timer` (weekly) | disabled at cutover |
| `live-checks.yml` | `kalshi-live-checks.timer` (daily) | disabled at cutover |
| `maintenance.yml` | `kalshi-maint` over SSH (below) | disabled at cutover |

- Workflow files are deleted in a separate commit after the 14-day rollback
  window.
- The Actions secrets `DATABASE_URL`, `KALSHI_*`, `TELEGRAM_*`,
  `ODDS_API_KEY` and `DEADMAN_PING_URL` are deleted at the same time.
- Also retired with Neon: `shrink_tape`, `vacuum_full`, the session job, and
  the Environments plan.

## 2. Logging in

Add this to `~/.ssh/config` on your Mac, once:

```
Host kalshi
    HostName <vm-public-ip>
    User ubuntu
    IdentityFile ~/.ssh/oci_kalshi
    UseKeychain yes
```

Then `ssh kalshi`. Dashboard: `ssh -L 8000:localhost:8000 kalshi`, then open
http://localhost:8000.

## 3. Maintenance actions

- `kalshi-maint` runs `python -m src.maintenance` as the `kalshi` user with
  the production environment loaded.
- `kalshi-run` does the same for any other module.

| Action | Command |
|---|---|
| DB census | `kalshi-maint --db-stats` |
| Polymarket queue | `kalshi-maint --pending-matches` |
| Day-7 measurement | `kalshi-run src.execution.day7` |
| Retention dry run | `kalshi-run src.maintenance.prune --dry-run` |
| Retention now | `kalshi-run src.maintenance.prune` |
| Wipe void shadow (dry run) | `kalshi-maint --wipe-void-shadow` |
| Wipe void shadow (execute) | `kalshi-maint --wipe-void-shadow --confirm WIPE-VOID-SHADOW` |
| Purge orphan markets (dry / execute) | `kalshi-maint --purge-markets` / `… --confirm PURGE-ORPHAN-MARKETS` |
| Retire a deploy (dry / execute) | `kalshi-maint --retire-sha <sha>` / `… --confirm RETIRE-DEPLOY-SHA` |
| Clear a latched halt (kill switch) | `kalshi-maint --clear-halt --confirm CLEAR-HALT` |
| One cycle by hand | `kalshi-run src.run_trading` (the cycle lock refuses an overlap) |
| Logs | `journalctl -u kalshi-cycle -n 200`, `journalctl -u kalshi-recorder -f` |
| Timers | `systemctl list-timers 'kalshi-*'` |

**Destructive actions stay human-typed.** `kalshi-maint` refuses any
`--confirm` unless it is attached to an interactive terminal (`[ -t 0 ]`), so
no timer, script or pasted pipeline can execute one. The confirm tokens are
the same as today. Dry runs need no token, as before.

## 4. Where secrets live

- `/etc/kalshi/env`, owner `kalshi`, mode `600`, readable only by the
  services.
- Edit with `sudoedit /etc/kalshi/env`, then
  `sudo systemctl restart kalshi-recorder`. The cycle picks changes up on its
  next run.
- Contents: `KALSHI_API_KEY`, `KALSHI_PRIVATE_KEY`, `TELEGRAM_TOKEN`,
  `TELEGRAM_CHAT_ID`, `ODDS_API_KEY`, `DEADMAN_PING_URL`, `DATABASE_URL`
  (local socket), `B2_KEY_ID`, `B2_APP_KEY`, `OCI_PAR_URL`, `AGE_RECIPIENT`
  (a public key).
- Never in the repo, never in a chat, never in shell history: type values in
  the editor.
- The age **private** key and the SSH **private** key are never on the VM.

## 5. Getting new code to the VM

The repo is public, so the VM reads it over HTTPS with **no GitHub
credential**.

```bash
ssh kalshi
kalshi-deploy <sha-or-main>
```

`kalshi-deploy`:
1. `git fetch` and check out the exact SHA in `/opt/kalshi/app`.
2. `pip install` into the venv.
3. Run the **full test suite against the local Postgres**. It stops the
   deploy if anything is red; the running version is untouched.
4. `python -m src.migrate`.
5. Record the SHA (the `deploy_sha` stamped on trades) and restart the
   recorder. The next cycle runs the new code.

Rollback is `kalshi-deploy <previous-sha>`, with `kalshi-deploy --history` to
list past SHAs. Nothing deploys itself: you choose the SHA, as today you
choose what to push.

## 6. If the VM is unreachable

Work down this list:

1. **Is it down or only SSH?** Check whether the healthchecks.io check is
   green (it pings from inside the VM) and whether Telegram is still
   receiving. If both are alive, it's an SSH problem: fix your IP (SPEC §2.4)
   or use the second admin path.
2. **Oracle console → Compute → Instances → `kalshi-paper`:**
   - **Stopped** → **Start**. Timers resume, missed cycles do not stack
     (`Persistent=true` runs one catch-up), and the settler reconciles every
     position from Kalshi.
   - **Running but dead** → **Reboot** (Actions menu).
   - **Gone (reclaimed or terminated)** → rebuild per BACKUP.md "Rebuilding
     production from a dump": new VM, then a restore from copy A (or copy B
     if the account itself is gone).
3. **Can't get a new VM** (no capacity): paper trading simply pauses; nothing
   is at risk. Within the 14-day rollback window, re-enable the GitHub
   schedules (Neon is intact). After it, restore copy B into a fresh Neon
   free project and re-enable the workflows, which still exist in git
   history.

**Not urgent, by design.** This is paper trading: an outage costs gate
accrual and evidence, never money. Positions settle on Kalshi whether or not
we are watching, and the settler catches up on the first cycle back.
