# Threat model for Agent-generated strategies

## Protected assets

Credentials, private account data, capital, position ownership, protection
orders, approval records, the execution ledger, risk policy, and runtime state.

## Trust boundaries

The operator owns approvals and risk policy. Strategies and future Agent output
are untrusted policy input. Only the exchange adapter knows SDK shapes; only the
execution coordinator holds order authority. Credentials remain in runtime
configuration and are never supplied to strategy code.

## Threats and controls

- Credential or file exfiltration: Agent output is a declarative spec, never loaded
  Python. Forbidden authority fields and unsafe strings are rejected. The worker sees
  only the trusted interpreter and one request in an isolated namespace with a clean
  environment, no repository/home mount and no network route.
- Risk bypass: strategies can emit only typed intent inputs. A3 requires a run
  lease and A4 requires a matching immutable risk decision before submission.
- Approval reuse: bundle, spec, evaluation, commit, environment, allocation policy,
  risk policy and expiry are bound; approval is consumed exactly once.
- Post-approval modification: any byte change, extra file, symlink or changed policy
  invalidates the content-addressed bundle or approval.
- State corruption or duplicate writer: hash-chained ledger, exclusive lease,
  fail-closed replay, and Supervisor health detection apply.
- Data leakage through observations: reports are aggregate and logs redact
  secret-shaped fields.

## Residual risks and eligibility

The OS sandbox, provenance, approval ledger and offline Supervisor are implemented and
red-team tested. Capability probes are host-specific and must repeat after deployment
changes. Arbitrary generated Python remains prohibited. External trust roots and the
operator capability require privileged deployment handling.

The readiness decision is PASS only for isolated offline candidates. The Agent cannot
approve itself, access execution, or start Testnet/live trading. Any such promotion is
a separate future stage with explicit operator approval.

## Investor accounting and email-query boundary

Investor identities, verified email addresses, balances, statements, source
events, and accounting backups are protected assets. The accounting service is
the sole ledger writer and has no order capability. The inbound email-query
process receives only read-only reconciled views and an isolated dedupe/rate
limit store; it has no accounting write interface, Binance credential, strategy
input, shell, SQL template, or administrative command path.

Exact command whitelisting, verified-address matching, attachment rejection,
message-ID dedupe, sender rate limiting, generic unauthorized responses, and
cross-investor tests constrain a compromised mailbox. Email is not an
authentication channel for deposits, withdrawals, strategy changes, trading,
or runtime control. SQLite files and reports remain sensitive even without API
keys and require restrictive permissions and protected backups.
