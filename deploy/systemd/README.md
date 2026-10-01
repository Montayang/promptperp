# PromptPerp systemd templates

These templates are deployment examples, not an activation script. Installing or
starting them requires a separate operator-approved production rollout.

Expected identities and paths:

- `promptperp-accounting`: only ledger writer; owns `/var/lib/promptperp/accounting`;
- `promptperp-mail`: mail provider; owns `/var/lib/promptperp/mail` and can update the
  report delivery outbox;
- `promptperp-query`: query provider; reads the ledger directory and writes only
  `/var/lib/promptperp/query`;
- all program files are installed read-only at `/opt/promptperp`;
- backups live on an encrypted filesystem at `/var/backups/promptperp`;
- mail-only `0600` environment files live under `/etc/promptperp` and must never
  contain Binance keys or be committed.

Grant `promptperp-query` read-only traversal/read access to the accounting directory
with an OS reader group or ACL. Do not give it ownership or write permission.
The unit also enforces the boundary with `ReadOnlyPaths`. The mail outbox worker
must update delivery state in the ledger and therefore gets narrowly scoped write
access to that directory, but it receives no trading environment file.

The scheduler only queues statements. The two mail units only connect when
`--execute` has a clean matching commit and a current approval window of at most
one hour. Put `COMMIT_SHA`, `APPROVED_AT`, and `APPROVAL_EXPIRES_AT` in the
corresponding owner-only approval environment file. Leaving that file absent keeps
real mail disabled. `SENDING` is never retried automatically.

The monitor exits with status 2 on stale reconciliation, outbox uncertainty or
backlog, quarantined events, unresolved owned orders, or an invalid/stale backup.
Connect the unit failure to the host's existing alert transport; no alert provider
or recipient is embedded here.

The deployment monitor similarly writes provider-neutral health, Prometheus and alert
spool files. `promptperp-alert@.service` remains inert unless the operator installs an
executable `/usr/local/libexec/promptperp-alert-bridge`.

The public distribution intentionally contains no live strategy service unit. Strategy
workers are deployment-specific extensions and must provide their own approval,
reconciliation, lease, recovery and bounded-restart controls.

See `docs/DEPLOYMENT.md`, `docs/RELEASE_CHECKLIST.md` and
`docs/DISASTER_RECOVERY.md` before any host rollout.
