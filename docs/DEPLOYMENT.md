# Deployment and release operations

This document defines the supported deployment model. Deployment commands only
prepare files and machine-readable state. They never start a strategy, call Binance,
send email, install systemd units, create host users, or create the live-enable marker.

## Filesystem layout

| Path | Owner / access | Purpose |
|---|---|---|
| `/opt/promptperp/releases/<fingerprint>` | deployment owner; runtime read-only | content-addressed artifact and installed runtime |
| `/opt/promptperp/current-{offline,testnet,production}` | deployment owner | atomic channel pointer |
| `/etc/promptperp` | root/operator | non-secret configuration and run approvals |
| `/etc/promptperp/credentials` | root plus exact service identity, `0700`/`0600` | service-specific credentials |
| `/var/lib/promptperp` | component-specific identities | databases, leases, checkpoints and status |
| `/var/log/promptperp` | component-specific identities | redacted service logs if not using journald |
| `/var/backups/promptperp` | backup identity, encrypted filesystem | verified state archives |
| `/var/lib/promptperp/alerts` | monitor writer, alert bridge reader | provider-neutral alert spool |

Code, configuration, secrets, mutable state, logs and backups are never combined.
The repository and `.git` directory are not deployed. A production runtime receives
only the installed wheel, its narrowly scoped configuration and the credential file
for that service.

## Identities

- `promptperp-deploy`: owns release directories and channel links; no exchange secret.
- `promptperp-trading`: reads the exchange credential and writes only strategy/execution
  state plus the explicitly shared accounting database.
- `promptperp-accounting`: sole normal ledger writer.
- `promptperp-mail` and `promptperp-query`: retain the existing isolated mail boundaries.
- `promptperp-monitor`: reads status and writes health, metrics and the alert spool.
- `promptperp-alert`: reads the spool and invokes the operator-installed alert bridge.
- `promptperp-backup`: writes the encrypted backup destination during an operator stop.

Use groups/ACLs only for explicitly shared files. Never solve a permission problem by
making `/etc/promptperp`, `/var/lib/promptperp`, or a database world-readable/writable.

## Build an offline release

On a network-enabled build machine using the target Python/platform:

1. Check out an exact clean commit and run `./scripts/quality.sh`.
2. Build the application wheel.
3. Download every pinned runtime dependency into `wheelhouse/wheels` with pip's
   download command. Do not build or download on the trading host.
4. Run `scripts/lock_wheelhouse.py` to create canonical `wheelhouse.json` hashes.
5. Create a `ReleaseManifest` binding semantic version, commit, application wheel,
   Python version and config/state schema versions.
6. Transfer the wheel, wheelhouse, manifest and approval/evidence through the approved
   artifact channel. Verify transport hashes before deployment.

The target-side `install` command creates a fresh virtual environment and forces pip
to use `--no-index`. Missing dependencies, extra/missing wheels, changed bytes,
dependency conflicts or failed `import promptperp` block installation. A failed install
never changes a channel pointer.

## Initial offline installation

The examples use a staging root. Use `--root /` only in an explicitly approved host
deployment:

```text
python -m promptperp.operations.deployment_service --root <staging-root> init
python -m promptperp.operations.deployment_service --root <staging-root> prepare ...
python -m promptperp.operations.deployment_service --root <staging-root> install ...
```

`prepare` is idempotent only when every immutable byte still matches. `install` is
one-shot per release fingerprint. The operator then records evidence and creates a
short-lived external HMAC approval. The signing key is supplied only to the signing
process and deployment verifier; it is never accepted on a command line, copied into
a release, or printed.

Activation requires a fresh attestation stating that services are stopped, account
reconciliation succeeded, and positions, open orders, pending settlement and
unresolved ownership are all zero. Production attestations must come from the
operator-controlled reconciliation procedure; fictional JSON is accepted only in
offline tests.

## Promotion gates

Channels cannot be skipped:

```text
same release fingerprint: OFFLINE -> TESTNET -> PRODUCTION
```

- OFFLINE requires a clean commit, full quality gate, sandbox pass and no findings.
- TESTNET additionally requires rollback and disaster-restore drills.
- PRODUCTION additionally requires the separately approved Testnet protocol result.
- Evidence binds the exact quality, sandbox, rollback, restore and (when applicable)
  Testnet report fingerprints; booleans without report hashes are rejected.
- Evidence is valid for at most 30 days.
- Approval windows are capped at 24 hours offline, 4 hours Testnet and 1 hour
  production; each approval ID is consumed once.
- A state-schema change is never automatically upgraded or rolled back. It needs a
  dedicated migration, source backup and ownership-preservation acceptance.

Promotion changes a symlink and hash-protected state only. It does not start a service.

## Starting and stopping live services

The public systemd templates cover accounting, mail and deployment monitoring only.
They are inert repository assets; installation, user creation, `daemon-reload`,
enablement and start require a separate operator decision. PromptPerp ships no live
strategy unit. A downstream strategy worker must document bounded restart, SIGTERM
handling, approval expiry, exclusive leases, reconciliation and recovery before use.

Do not remove state, ownership or protection records while any position may exist.

## Credentials and rotation

`rotate-credentials` reads a root-owned source file; secret values are never command
arguments or output. Exact key allowlists prevent one service receiving another
service's secret. Receipts contain only service, generation, key names and time.
Exchange credential rotation requires the same fresh stopped/flat/reconciled
attestation as a release mutation.

After rotation, verify public/read-only connectivity under a separate approval before
any live restart. Retire the old exchange key only after the new key is confirmed and
the rollback window is closed. Rotation files are excluded from state archives.

## Monitoring and external alerts

Components publish canonical, non-sensitive health files. The monitor produces:

- `/var/lib/promptperp/status/health.json`;
- Prometheus textfile metrics at `metrics.prom`;
- a static local `dashboard.html`;
- deduplicated provider-neutral events in `alerts/outbox.ndjson`.

Blocked/stale/corrupt state makes the monitor exit 2. The optional
`promptperp-alert@.service` calls an operator-installed
`/usr/local/libexec/promptperp-alert-bridge`; this repository embeds no recipient,
provider credential or outbound alert implementation.

## Upgrade and rollback

1. Stop services and prevent restart.
2. Reconcile until the safety attestation is fully clear.
3. Create and verify a state archive.
4. Prepare/install/promote the new release through each channel.
5. Inspect machine health before separately starting any runtime.
6. If validation fails, keep services stopped and activate `rollback` with a new,
   one-use approval for the previous release.
7. Re-run reconciliation and only then consider a separate live start.

Rollback preserves the failed release, previous release, generation, approval and
hash-chained history. It never deletes state or silently crosses a state schema.
An interrupted filesystem transaction leaves `deployment-transaction.json`; monitoring
then reports `BLOCKED`, further mutation is refused, and the operator must compare the
channel link, hash-protected state and history before choosing a recovery action.
