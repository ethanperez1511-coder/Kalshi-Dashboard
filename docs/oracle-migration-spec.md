# Oracle Cloud migration: spec (for approval, nothing built)

**Status:** draft for operator approval, 2026-10-07. Nothing has been
provisioned or changed. The account is Always Free, home region US East
(Ashburn), not upgraded, with no resources created.

**Why move:** GitHub's scheduler delivered ~5–8 of 96 cycles a day, and the
session's triggers fired 0 of 10 on days 1–2. Neon's caps (1 GB storage,
100 CU-h, 5 GB egress) shaped every design decision for two months. A VM
running Postgres, the pipeline and the recorder removes all three constraints:
cron is local and exact, and storage and compute are not metered per use.

---

## 0. Facts this spec depends on (verified 2026-10-07)

| Fact | Source |
|---|---|
| A1 Always Free: "first 1,500 OCPU hours and 9,000 GB hours per month … equivalent to **2 OCPUs and 12 GB** of memory" (cut from 4/24 on 2026-06-15) | [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm); [community report of the cut](https://medium.com/@imvinojanv/setup-always-free-vps-with-4-ocpu-24gb-ram-and-200gb-storage-the-ultimate-oracle-cloud-guide-bed5cbf73d34) |
| E2.1.Micro: up to two, each **1/8 OCPU** (burstable) and **1 GB** | same |
| Block storage: **200 GB total**, five volume backups; **minimum boot volume 47 GB** | same |
| Object Storage: 20 GB | same |
| **Idle reclamation:** an instance "may be reclaimed" if, over 7 days, CPU p95 < 20% **and** network < 20% **and** memory < 20% (memory criterion on A1 only) | same |
| "Out of host capacity" is a temporary shortage: "try a different availability domain, or wait … then try again" | same |
| SSH key types accepted: RSA, DSA, DSS, ECDSA, **Ed25519** | [Managing key pairs](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/managingkeypairs.htm) |

**Not verified, do not rely on:**
- That upgrading to Pay As You Go exempts an instance from idle reclamation.
  It's widely repeated, but the Always Free page does not say it.
- Whether a reclaimed instance is stopped or terminated.
- The default username, `ubuntu` on Ubuntu images. That is standard on OCI
  but was not in the page I fetched; the console shows it on the instance page.

---

## 1. Sizing

**Load:**
- Postgres: today's database is ~740 MB file, ~380 MB live.
- The pipeline: one Python process every 15 min, ingesting ~7k markets and
  scoring ~250.
- The recorder: a long-running websocket process.
- Light dependencies (FastAPI, SQLAlchemy, httpx, psycopg); no numpy/pandas.

**Recommended: VM.Standard.A1.Flex, 2 OCPU / 12 GB** (the whole Always Free A1
allowance, in one VM).

| Component | Expected resident memory |
|---|---|
| OS + sshd + journald | ~300 MB |
| Postgres 16, shared_buffers 2 GB (16% of RAM) | up to ~2.3 GB as the cache warms |
| Pipeline cycle (peak, during ingest) | ~300–500 MB |
| Recorder | ~100–150 MB |
| **Total** | **~3 GB of 12**, leaving room for page cache and the dashboard |

**Is the 1 GB AMD micro shape too small? Yes, for this workload.**
- **Memory:** OS (~300 MB) + Postgres trimmed to ~250 MB + a cycle peak of
  ~400 MB + the recorder (~100 MB) is already ~1 GB, with no page cache. An
  ingest spike would be killed by the OOM killer, possibly mid-write.
- **CPU:** 1/8 OCPU baseline. A cycle that takes ~3 minutes on GitHub's 2-vCPU
  runner would take far longer on a burstable 1/8 core, and could overrun its
  own 15-minute interval.
- **Verdict:** not viable for Postgres + pipeline + recorder. Its only use is
  the hybrid fallback in §3.

**Idle reclamation is the main risk on A1, not capacity.**
- Memory: at ~3 GB of 12, utilisation is ~25%. That is near the 20% line, not
  safely above it. Page cache may not count as "used".
- CPU: if cycles run only in the 15–21 UTC session, the VM is busy ~5% of the
  day, so CPU p95 will be under 20%. All three criteria would plausibly hold,
  and the VM would be reclaimable.
- **Mitigation built into this design:** on the VM, cycles run every 15
  minutes **around the clock**, the original cadence. The "zero off-session
  cycles" rule existed to save Neon compute, which no longer applies.
  - 96 cycles a day at ~2–3 min each puts CPU p95 well above 20% from real
    work.
  - Reclamation needs all three criteria to hold, so CPU alone keeps the VM
    out of it. No artificial load.
- **Proof, not assumption:** after week 1, read the instance's CPU metric
  (Compute → instance → Metrics). If CPU p95 is under 25%, escalate:
  1. Drop to **1 OCPU / 6 GB**, which roughly doubles CPU % and memory %.
  2. Or upgrade to Pay As You Go, after confirming the exemption in writing
     from Oracle.
- **Backups (below) make reclamation recoverable either way.**

---

## 2. Console steps: create the VM

UI labels move between console releases. Where a label differs, look for the
same concept.

### 2.1 SSH key, generated on your Mac (the private key never leaves it)

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/oci_kalshi -C "kalshi-oci"
#   enter a passphrase when asked (stored in macOS Keychain below)
ssh-add --apple-use-keychain ~/.ssh/oci_kalshi
pbcopy < ~/.ssh/oci_kalshi.pub      # copies the PUBLIC key only
```

- `~/.ssh/oci_kalshi` is the **private** key. It never goes into a browser, a
  chat, a repo or a secret store.
- `~/.ssh/oci_kalshi.pub` is the **public** key, the only thing you paste into
  the console.
- Do **not** use the console's "Generate a key pair for me". It creates the
  private key in the browser and downloads it.

### 2.2 Create the instance

1. Sign in and check the region selector (top right) says **US East (Ashburn)**.
2. ☰ menu → **Compute** → **Instances**. Compartment: your root compartment.
   Click **Create instance**.
3. **Name:** `kalshi-paper`.
4. **Placement:** Availability domain **AD-1** (see §3 if out of capacity).
   Capacity type: on-demand (the default).
5. **Image and shape** → **Edit**:
   - **Change image** → **Ubuntu** → **Canonical Ubuntu 24.04**. Not
     "Minimal": the full image has the tooling we need. The aarch64 build is
     selected automatically once the shape is Ampere.
   - **Change shape** → **Virtual machine** → **Ampere** →
     **VM.Standard.A1.Flex** → **OCPUs: 2**, **Memory: 12 GB**. Confirm the
     **"Always Free-eligible"** tag is shown.
6. **Networking:**
   - **Create new virtual cloud network** and **Create new public subnet**
     (defaults are fine).
   - **Assign a public IPv4 address: Yes.**
7. **Add SSH keys:** **Paste public keys**, then paste (⌘V) the `.pub`
   contents from 2.1. One line, starting `ssh-ed25519`.
8. **Boot volume:**
   - **Specify a custom boot volume size: 100 GB.**
     - It must be at least 47 GB, and the free allowance is 200 GB total.
     - 100 GB leaves 100 GB free for a later data volume or headroom.
   - Leave performance at the default (Balanced, 10 VPU/GB) and encryption
     Oracle-managed.
   - Leave in-transit encryption at its default.
9. **Create.** Wait for **Running** and note the **Public IP address** and
   **Username** shown on the instance page.

### 2.3 Ports (open exactly one)

1. On the instance page → the **subnet** link → its **Default Security List**
   → **Ingress Rules**.
2. There is a default rule for **TCP 22 from 0.0.0.0/0**. **Edit it: Source
   CIDR = your home IP /32.**
   - Find your IP with `curl -4 ifconfig.me`.
   - If your home IP changes, SSH stops working until you edit this rule to
     the new /32 in the web console, which is always reachable. That is the
     whole recovery path: Cloud Shell would be blocked by the same rule, and
     the serial console needs a password the image does not set.
3. Leave the default ICMP rules.
4. **Open nothing else.** No 5432 (Postgres listens on localhost only), no
   8000/80/443 (the dashboard is reached over an SSH tunnel:
   `ssh -L 8000:localhost:8000 -i ~/.ssh/oci_kalshi ubuntu@<ip>`).
5. Egress stays the default allow-all. The VM must reach Kalshi, NOAA/IEM,
   Polymarket, Telegram and healthchecks.io.
6. OCI's Ubuntu image also ships a host firewall (iptables) that allows only
   SSH. Leave it; don't enable ufw on top of it.

### 2.4 First login check

```bash
ssh -i ~/.ssh/oci_kalshi ubuntu@<public-ip>
uname -m          # aarch64
nproc; free -g    # 2 / ~11
```

### 2.5 Cost guard (free, belt and braces)

☰ → **Billing & Cost Management** → **Budgets** → create a budget of **$1**
with an email alert at 100%. On an un-upgraded account nothing can bill, but
the alert costs nothing if that ever changes.

---

## 3. Fallback when Ampere is "Out of host capacity"

Ashburn A1 capacity is often exhausted. In order:

1. **Other availability domains.** Repeat 2.2 with **AD-2**, then **AD-3**.
   Each AD has independent capacity.
2. **Smaller shape:** **1 OCPU / 6 GB** in each AD. Enough for this workload
   (~3 GB resident), and smaller requests land more often. Resizing up later
   needs a stop/start and its own capacity.
3. **Retry over time:** capacity frees up unpredictably, and off-peak US hours
   are often better. Retrying the console once or twice a day for a few days
   is fine.
   - A scripted retry with the OCI CLI is possible, but it needs an OCI API
     signing key on your Mac. Your call; not required.
4. **Hybrid, only if A1 stays unobtainable for a week or more:**
   - one **E2.1.Micro** (1 GB) runs **only the pipeline and the recorder**,
     with a 2 GB swap file;
   - Postgres stays on **Neon**.
   - This fixes the GitHub scheduler problem but not Neon's caps, and a cycle
     on 1/8 OCPU must be measured before trusting the 15-minute cadence.
   - Strictly second best.
5. **Pay As You Go** is reported (unverified) to improve A1 capacity access.
   It needs a card and stays $0 inside Always Free limits. Only with a budget
   alert, and only by your decision.

**Never:** put Postgres + pipeline + recorder on the 1 GB micro (§1).

---

## 4. Migration plan (after the VM exists)

### Phase B: base install (reversible, touches nothing in production)
- Create a `kalshi` system user.
- Install Python 3.12 (Ubuntu 24.04 default) in a venv, and Postgres 16 from
  Ubuntu apt with `listen_addresses = 'localhost'`.
- Postgres tuning: `shared_buffers = 2GB`, `effective_cache_size = 6GB`,
  autovacuum on (it now runs continuously, unlike on scale-to-zero Neon).
- `unattended-upgrades` for security patches.
- Secrets live in `/etc/kalshi/env`, owner `kalshi`, mode 600, typed on the VM
  over SSH. The same set as the Actions secrets:
  - `KALSHI_*`, `TELEGRAM_*`, `DEADMAN_PING_URL`
  - `ODDS_API_KEY` stays **unset until cutover** (it is shared quota)
  - `DATABASE_URL` = local Postgres

### Phase C: services (systemd, not cron)
| Unit | Schedule |
|---|---|
| `kalshi-cycle.timer` | every 15 min, 24/7, `Persistent=true`. The existing `cycle_lock` still guarantees one cycle at a time. |
| `kalshi-recorder.service` | continuous, restarted hourly (refreshes subscriptions). Recording window: 15–21 UTC or 24/7, ruled at cutover from measured disk use. |
| `kalshi-retention.timer` | daily 04:25 UTC |
| `kalshi-refit.timer` | weekly, Monday |
| `kalshi-live-checks.timer` | daily 13:17 UTC |

The cycle-count digest line and the dead-man ping work unchanged.

### Phase D: data and cutover (by overlap, L32)
1. **Seed.** On the VM, `pg_dump` from Neon and `pg_restore` locally. You type
   the Neon connection string into the VM's shell; it never goes in a chat or
   the repo. About 740 MB, within Neon's 5 GB egress.
2. **Overlap, 7 days.** The VM runs every service on its **own copy**.
   GitHub + Neon stay canonical.
   - Its Telegram goes to a separate chat (or is off), so alerts are never
     doubled.
   - `ODDS_API_KEY` stays unset, so the shared Odds quota is not spent twice.
   - VM trades during overlap are validation only and are **never** merged
     into the canonical record.
   - Compare the VM's digests with GitHub's daily.
3. **Cutover (one sitting, about 30 min):**
   1. Disable every GitHub schedule (keep the files).
   2. Wait for any running Actions job to finish.
   3. Take a final `pg_dump` from Neon and restore it into a fresh VM database.
   4. Check the gate count and open positions match Neon exactly.
   5. Switch the VM's Telegram to the main chat, set `ODDS_API_KEY`, and
      start the timers.
4. **Rollback (any time in the first 14 days):** stop the VM timers and
   re-enable the GitHub schedules. Neon is not modified after cutover and is
   kept untouched for 14 days.

### Backups (reclamation and disk failure)
- **Nightly `pg_dump`** → OCI Object Storage (20 GB free). Upload through a
  **write-only pre-authenticated request URL**, so the VM holds no OCI API
  credentials. Keep 14 dumps.
- **Weekly boot-volume backup** (five free).
- **A restore drill** is part of acceptance: restore last night's dump into a
  scratch database and compare row counts.

---

## 5. Safety (CLAUDE.md hard constraints)

- **Paper trading stays the default.** `mode` lives in the database and is
  copied with it; nothing in this plan writes it. The live gate (`mode ==
  "live"` AND ≥ 50 paper trades, flipped by a human) is untouched.
- **Risk limits are unchanged.** Same code, same constants. No risk file is
  touched.
- **Database integrity: there is never more than one canonical writer.**
  - During overlap, the VM writes only its own copy.
  - At cutover, GitHub is disabled **before** the final dump, and the VM
    starts only after the restore is verified.
  - `cycle_lock` still serialises cycles on the VM.
- **No fallback data.** Same feeds and the same fail-safe refusals; the move
  changes where code runs, not what it trusts.
- **New exposure: the Kalshi API key sits at rest on a VM.**
  - Mitigated by SSH key-only access from one IP, no other open ports, a 600
    env file and automatic security updates.
  - If Kalshi offers read-only API keys, use one until a live flip is ever
    ruled.

---

## 6. Acceptance (before cutover)

- [ ] Seven overlap days: the VM's "Cycles 24h" line shows ≥ 90 cycles a day
      on the VM's own copy.
- [ ] The recorder's coverage matches the configured window, with no
      unexplained gaps.
- [ ] The dead-man check stays green through the overlap.
- [ ] The CPU p95 metric (OCI console) is ≥ 25%, or a ruling on 1 OCPU or PAYG
      has been made.
- [ ] A restore drill from the nightly dump has passed.
- [ ] The full test suite has been run ON THE VM against its local Postgres
      (`TEST_POSTGRES_URL`), including the 10 Postgres-only tests.

---

## 7. Decisions for the operator

1. Approve the shape (2 OCPU / 12 GB) and the 24/7 cycle cadence on the VM
   (the counter to idle reclamation).
2. Recorder window on the VM: 15–21 UTC as now, or 24/7? Storage stops
   binding at 100 GB, so this is about evidence per hour, not cost.
3. Whether to upgrade to Pay As You Go: not needed to start. Revisit only if
   capacity or the week-1 CPU metric forces it.
