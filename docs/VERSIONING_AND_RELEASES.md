# Versioning and releases

The project follows Semantic Versioning for supported public interfaces.

- PATCH: compatible fixes and safety hardening with no intentional API change.
- MINOR: compatible strategy/runtime features or new schema versions.
- MAJOR: incompatible API, persisted-state, approval, or policy changes.

Persisted schemas have their own integer version. Readers fail closed on an
unknown version. A release that changes state must include forward migration,
rollback instructions, and ownership-preservation evidence.

## Release gate

1. Start from a clean main commit with the offline quality gate passing.
2. Review dependency and repository scans.
3. Confirm documentation, changelog, and version agree.
4. Build wheel and source distribution from the locked environment.
5. Inspect artifacts for credentials and runtime files.
6. Create a signed tag and publish artifacts through the approved channel.

The deployable release additionally requires a target-platform offline wheelhouse and
canonical hash lock. `ReleaseManifest` binds the application artifact, commit, Python,
config schema and state schema. The same release fingerprint must pass OFFLINE and
TESTNET before PRODUCTION. Promotion approvals are environment-specific, short-lived
and single-use. See `RELEASE_CHECKLIST.md`.

Automatic upgrade/rollback across state schema versions is prohibited. Such a release
must provide an explicit forward migration, source backup, rollback transformation and
ownership-preservation acceptance before promotion.

A release never authorizes a live run. Every live run remains bound to its
separate strategy, commit, parameters, environment, and risk-policy approval.
