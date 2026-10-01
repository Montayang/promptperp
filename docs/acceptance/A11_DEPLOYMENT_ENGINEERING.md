# A11 deployment engineering acceptance

Date: 2026-10-01 (Asia/Singapore)

Scope: completely offline temporary host roots and fictional artifacts. No host users,
systemd units, live marker, private API, Testnet, strategy, email or external alert was
started.

## Results

- Clean layout creation separates code, config, credentials, state, logs and backups.
- A locked wheelhouse creates a fresh virtual environment with pip `--no-index`, then
  passes dependency and import checks.
- Content changes, extra wheels and incomplete installs fail closed.
- OFFLINE -> TESTNET -> PRODUCTION ordering is enforced for the same fingerprint.
- Promotion binds fresh evidence, short-lived one-use approval and a flat reconciled
  stop; changed/signature-invalid/stale/replayed input is rejected.
- Atomic channel switching records hash-protected state and hash-chained history.
- Upgrade/rollback drill preserves both releases and forbids state-schema crossing.
- Credential rotation uses exact per-service keys, restrictive modes and no secret in
  receipts, reports or backups.
- Backup/restore checks every path, size and digest, excludes ephemeral/secret files,
  rejects non-empty targets and publishes only after full extraction succeeds.
- Health JSON, Prometheus metrics, local dashboard and deduplicated external alert
  spool are generated without account values or credentials.
- The live systemd template is opt-in, restart-bounded and hardened; it was inspected
  only and not installed.

Decision: deployment engineering is accepted for offline packaging and operator-led
rollout. This acceptance does not approve Testnet or production deployment. Actual
host installation, private reconciliation, service enablement and live start remain
separately authorized actions.
