# Supervised runtime operations

The standard runtime is controlled through a narrow lifecycle surface:
`start`, `heartbeat`, `status`, and `stop`. These operations do not grant a
strategy exchange credentials or an exchange client.

## Safety model

- `start` acquires an exclusive run lease before publishing RUNNING.
- A second writer is rejected.
- `heartbeat` records health and aggregate counters; it never stores credentials.
- `status` is read-only and detects stale heartbeats or a missing writer.
- `stop` disables new entries. A flat run becomes STOPPED.
- If an owned position exists, `stop` records STOP_REQUESTED and preserves
  ownership and protection flags. It does not cancel orders, close a position,
  or delete state.

Health values are NORMAL, ENTRY_DISABLED, RECONCILIATION_REQUIRED, BLOCKED,
and STOPPED. Unknown order outcomes, inconsistent state, incomplete protection,
or reconciliation failure must be represented as BLOCKED or
RECONCILIATION_REQUIRED before any further opening intent.

## Crash and recovery procedure

1. Use the read-only status inspection.
2. If the heartbeat is stale or the writer lease is absent, treat the run as
   blocked.
3. Verify the append-only execution ledger.
4. Reconcile owned exchange objects using the A3 recovery coordinator.
5. Preserve foreign positions and all unresolved ownership.
6. Only a newly approved writer may acquire the lease and resume.

Status inspection must never be used as an account query. Testnet and live
protocol checks still require separate operator approval.

Host installation, version promotion and rollback are separate from this runtime
lifecycle. They require stopped services and a fresh authoritative flat/reconciled
attestation. Deployment promotion never starts a runtime. See `DEPLOYMENT.md`.

## Observability and reports

Structured logs recursively redact credential-shaped keys and inline secret
assignments. Status files contain run identity, health flags, timestamps, and
small aggregate integer metrics only. They do not contain balances, credentials,
raw account payloads, or exchange order payloads.

Run reports are schema-versioned JSON with attributed aggregate PnL, commission,
funding, counters, final health, and unresolved-ownership status. A report never
claims safe completion when ownership remains unresolved.
