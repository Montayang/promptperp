# Security policy

## Supported versions

PromptPerp is pre-release software. Only the latest `main` revision is supported until
the first tagged release. No version or fix constitutes authorization for live trading.

## Reporting a vulnerability

Use a private GitHub security advisory for vulnerabilities. Do not open a public issue
containing exploit details, credentials, account information, order data, investor
data, runtime state or host information.

Include the affected revision, impact, a minimal offline reproduction and suggested
mitigation when possible. Never reproduce a report against a real account without the
account owner's explicit authorization.

## Operational response

If a trading-safety problem is suspected, stop the affected deployment through its
normal supervised procedure and prevent automatic restart. Do not delete persisted
state or exchange-hosted protection orders while a position may exist. Preserve
ownership and reconciliation evidence for recovery.

## Scope boundary

The bundled examples and quality gate are offline-only. The public distribution has no
live strategy service. Downstream live integrations, credentials, generated strategies
and operator configurations remain the deployer's responsibility.
