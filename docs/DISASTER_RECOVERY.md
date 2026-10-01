# Disaster recovery runbook

## Backup boundary

The deployment archive contains `/var/lib/promptperp` state only. It intentionally
excludes credentials, configuration, code, Git data, logs and the backup directory.
Lease, lock and socket files are also excluded. Backups require all services stopped,
an authoritative flat/reconciled account and no pending settlement or unresolved
ownership.

The archive has a canonical manifest with path, size and SHA-256 for every file.
Restore rejects symlinks, path traversal, extra/missing entries, malformed manifests,
hash changes, excessive size and non-empty targets. Extraction happens in a temporary
directory and becomes visible only after complete verification.

Store the archive on an encrypted filesystem and replicate it through an independently
approved channel. Credentials need a separate secret-manager backup and rotation
procedure; never put them into the state archive.

## Restore drill

1. Provision a clean compatible host and the least-privilege identities.
2. Prepare and offline-install the exact content-addressed release from the locked
   wheelhouse. Do not create the live-enable marker.
3. Verify the archive hash received from the backup catalog.
4. Restore into an empty staging state directory.
5. Run database-specific integrity checks and deployment monitor checks.
6. Verify release, config schema and state schema compatibility.
7. Confirm no credential or lease file exists in restored state.
8. Record the drill evidence and destroy the isolated restored copy if it was only a
   test.

## Production recovery

If the original host failed, keep all automatic starts disabled. Restore and inspect
locally first. Then use separately approved Binance read-only reconciliation to prove
actual positions, open/algo orders and ownership. Any mismatch, unknown order,
unprotected position or pending settlement stays BLOCKED and follows the execution
recovery runbook. A restored database is never evidence that the exchange is flat.

Only after state integrity, release compatibility and exchange reconciliation all
pass may the operator create new promotion/run approvals. Recovery does not reuse an
old live approval.
