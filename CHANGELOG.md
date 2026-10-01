# Changelog

**English** | [简体中文](CHANGELOG.zh-CN.md)

All notable changes to PromptPerp will be documented here. The project follows
Semantic Versioning after the first tagged release.

## [Unreleased]

### Added

- Initial public alpha candidate with a clean Git history.
- StrategySpec validation, deterministic signal interpretation and offline evaluation.
- Sandboxed candidate execution and single-use operator approvals.
- Recoverable execution, portfolio risk, accounting and deployment control planes.
- One offline-only `threshold_momentum` example strategy.

### Security

- Updated `aiohttp`, `idna` and `urllib3` to versions with no known vulnerabilities
  in the audited lock snapshot.
- Pinned GitHub Actions to reviewed immutable release commits.
- The public distribution excludes proprietary strategies and live strategy services.
- Tests block network and SMTP access and scan source and artifacts for secret-like
  material.
