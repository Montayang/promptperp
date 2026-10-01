# Release checklist

## Build

- [ ] Version and changelog updated; clean commit recorded.
- [ ] Full offline quality gate passes on the release commit.
- [ ] Application wheel and sdist inspected for secrets/runtime files.
- [ ] Target-platform dependency wheels downloaded from approved sources.
- [ ] Canonical wheelhouse lock created and independently hash-checked.
- [ ] Release manifest binds version, commit, Python and schema versions.

## Offline and Testnet promotion

- [ ] Fresh clean-host offline install succeeds with network disabled.
- [ ] Offline examples, sandbox probe, monitor and restore drill pass.
- [ ] Upgrade and rollback drill preserves previous release/state.
- [ ] No unresolved findings remain.
- [ ] Same release fingerprint promoted to OFFLINE.
- [ ] Separately approved Testnet protocol acceptance completed for the same release.
- [ ] Same release fingerprint promoted to TESTNET.

## Production change

- [ ] Live services stopped and automatic restart prevented.
- [ ] Authoritative reconciliation proves zero positions/orders/pending ownership.
- [ ] Fresh verified state archive stored on encrypted backup media.
- [ ] Production approval binds exact release/evidence and is within one hour.
- [ ] Release promoted without starting services.
- [ ] Machine health and deployment history inspected.
- [ ] Live-enable marker/run approval reviewed separately, if a start is intended.

## Rollback or recovery

- [ ] Services remain stopped.
- [ ] New flat/reconciled attestation obtained.
- [ ] New one-use approval binds the previous release.
- [ ] State schema matches; otherwise use dedicated migration recovery.
- [ ] Post-rollback integrity and exchange reconciliation pass before any restart.
- [ ] Changelog, incident record and machine history retained.
