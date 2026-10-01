# Changelog

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

- The public distribution excludes proprietary strategies and live strategy services.
- Tests block network and SMTP access and scan source and artifacts for secret-like
  material.
