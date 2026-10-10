# Changelog

**English** | [简体中文](CHANGELOG.zh-CN.md)

All notable changes to PromptPerp will be documented here. The project follows
Semantic Versioning after the first tagged release.

## [Unreleased]

### Fixed

- Bounded, identity-checked read-only confirmation of incomplete FILLED acknowledgements.
- Added opt-in exact post-fill convergence with stable refresh scope, durable request
  budget admission and restart-safe settlement holds, with synthetic regression tests.
- Documented integration requirements and non-universal policy decisions in both languages.

### Added

- Generic virtual position ownership, fill recovery and balanced basket execution.
- Shared account locking with distinct contention and reconciliation failures.
- Explicit-baseline equity history, offline curves and scheduled delivery callbacks.
- Bilingual integration and migration guidance for the new library components.

- Initial public alpha candidate with a clean Git history.
- StrategySpec validation, deterministic signal interpretation and offline evaluation.
- Sandboxed candidate execution and single-use operator approvals.
- Recoverable execution, portfolio risk, accounting and deployment control planes.
- One offline-only `threshold_momentum` example strategy.

### Security

- Update `multidict` to 6.9.1, `setuptools` to 83.0.0 and `wheel` to 0.46.2
  for CVE-2026-104874, CVE-2026-59890 and CVE-2026-24049 respectively.
  Require the patched build-tool versions for isolated source builds as well.

- Added restricted API-permission checks and stricter uncertain-order classification.

- Updated `aiohttp`, `idna` and `urllib3` to versions with no known vulnerabilities
  in the audited lock snapshot.
- Pinned GitHub Actions to reviewed immutable release commits.
- The public distribution excludes proprietary strategies and live strategy services.
- Tests block network and SMTP access and scan source and artifacts for secret-like
  material.
