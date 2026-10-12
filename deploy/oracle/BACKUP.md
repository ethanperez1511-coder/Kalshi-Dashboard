# Backups and restore (Oracle VM)

**The threat model decides the design.** Oracle can reclaim an idle Always
Free instance or terminate a whole account, and backups inside that account
die with it. So there are two copies, in two places, and only one of them
depends on Oracle.

**Ruled 2026-10-12:** weekly, with a tested restore, plus a monthly copy to
the Mac. The nightly Oracle-hosted copy is dropped: it died with the account
in exactly the case that matters.

| Copy | Where | Cadence | Retention | Survives account loss |
|---|---|---|---|---|
| B. Weekly dump | **Backblaze B2** (10 GB free tier), encrypted | Sunday 04:00 UTC | 8 weeks | **yes** |
| C. Monthly | your Mac, pulled by you | monthly | as you like | yes |

The boot-volume backups (five free) are a convenience for a fast rebuild,
**not** a backup: they live in the same account.

## What a dump is

```
pg_dump --format=custom --compress=9 kalshi > kalshi-YYYYMMDD.dump
```

It comes with `manifest.txt`: row counts per table, the database size, the
deploy SHA, the gate count, and the open-position count. That is what the
restore drill checks against.

**Encryption (copies B and C): [age](https://age-encryption.org)**
- Only the **public** key (`age1…`) is on the VM.
- The private key lives on your Mac, plus one offline copy (a password
  manager or printed).
- So a compromised VM can write backups but **cannot read** any of them, and
  a compromised B2 key reveals nothing.

```bash
# on your Mac, once
brew install age
age-keygen -o ~/.config/kalshi-backup.agekey     # prints the public key
```

## Copy B: Backblaze B2, independent of Oracle
1. Create a B2 bucket `kalshi-weekly`, **private**, with **Object Lock /
   file versions kept for 60 days** (so even an overwrite cannot destroy a
   week).
2. Create an **application key restricted to that bucket** with **write-only
   capability** (`writeFiles`; no `deleteFiles`, no `readFiles`). Confirm the
   exact capability names in the B2 console when creating it.
3. The key goes in `/etc/kalshi/env` as `B2_KEY_ID` / `B2_APP_KEY`, upload
   only. A leaked key can add files but not read, list-and-delete, or
   overwrite history.
4. The weekly timer uploads the same encrypted dump plus `manifest.txt.age`.

## Copy C: your Mac, monthly, by hand
```bash
scp -i ~/.ssh/oci_kalshi ubuntu@<vm-ip>:/var/backups/kalshi/latest.dump.age ~/kalshi-backups/
```
This is the copy that exists even if both cloud accounts are gone.

## Restore procedure (also the drill)

Runs on your Mac. The private key never goes to a server.

```bash
# 1. Fetch the newest dump (B2 web UI download, or copy C)
age -d -i ~/.config/kalshi-backup.agekey kalshi-YYYYMMDD.dump.age > kalshi.dump
age -d -i ~/.config/kalshi-backup.agekey manifest.txt.age > manifest.txt

# 2. Scratch Postgres 16
docker run -d --rm --name restore -e POSTGRES_PASSWORD=pw -p 55433:5432 --shm-size=512m postgres:16
createdb -h localhost -p 55433 -U postgres kalshi      # password: pw
pg_restore -h localhost -p 55433 -U postgres -d kalshi --no-owner kalshi.dump

# 3. Verify against the manifest (the repo carries the checker)
python -m deploy.oracle.verify_restore --manifest manifest.txt \
    --url postgresql+psycopg://postgres:pw@localhost:55433/kalshi
#    PASS = every table's row count matches, gate count and open positions match.

docker stop restore
```

**Rebuilding production from a dump:**
1. A new VM per SPEC §2.
2. Phase B install.
3. Copy the decrypted `kalshi.dump` up with `scp`, run `pg_restore` into the
   local Postgres, then run `verify_restore` against it. (Worst-case data loss
   is one week of tape and paper record; Kalshi holds the true state of
   every position.)
4. Start the timers.

Because it is paper trading, an outage pauses trading and nothing more. Kalshi
holds the true state of every position, and the settler reconciles on the
first cycle.

## Drill schedule
- **Before cutover:** one full drill from copy B. This is an acceptance
  criterion in SPEC §6.
- **Then monthly**, the first Sunday, logged in `tasks/todo.md` with the
  manifest numbers. A backup that has never been restored is a hope, not a
  backup.

## To build (Phase C, on this branch)
- [ ] `deploy/oracle/backup.sh`: dump, write the manifest, age-encrypt,
      upload to B2, and keep the latest locally for the monthly pull. It fails
      loudly, and the dead-man check gets a `/fail` ping.
- [ ] `deploy/oracle/verify_restore.py`: compares the manifest with the
      restored database.
- [ ] systemd timer `kalshi-backup-weekly`.
