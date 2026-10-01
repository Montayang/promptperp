# Architecture

The repository separates policy from effects.

1. A strategy receives immutable market snapshots and emits a `StrategySignal`.
2. Integration code quantizes the signal into a domain `TradeIntent`.
3. The operator-owned risk engine returns an immutable `RiskDecision`.
4. Only the execution coordinator can call the exchange port.
5. The append-only ledger and run lease preserve ownership across restart.
6. The supervisor publishes health without querying or mutating an account.

Strategies must not import exchange clients, configuration secrets, environment
loaders, notification clients, or runtime files. Exchange SDK objects stop at
the adapter boundary. Unknown order outcomes, foreign objects, reconciliation
failures, and incomplete protection fail closed.

The public modules are `config`, `domain`, `exchange`, `execution`,
`risk`, `runtime`, `strategies`, `strategy_spec`, `signal_engine`, `evaluation`,
`strategy_packages`, `approvals`, `sandbox`, `agent_pipeline`, `deployment`, and
`operations`. Proprietary strategy implementations and historical compatibility
clients are intentionally absent from the public distribution.

The `runtime` control plane sits above per-intent risk and execution. It binds
versioned plugins to persistent run plans, allocates capital per strategy,
serializes symbol ownership, aggregates shared-account exposure, and applies the
global `NORMAL`, `REDUCE_ONLY`, or `KILL_SWITCH` mode. A proposal must pass both
the portfolio gate and the existing operator-owned execution risk engine before
an exchange adapter may receive it. See
[`MULTI_STRATEGY_PLATFORM.md`](MULTI_STRATEGY_PLATFORM.md).

The Agent pipeline is an offline producer of candidates, not an execution path.
Untrusted descriptions become a declarative spec, deterministic report and
content-addressed bundle. A separately held operator capability grants an exact,
expiring, one-use approval. The Linux sandbox runs only the trusted interpreter;
allocation is added after isolation and the resulting risk request explicitly denies
execution. It has no dependency on exchange or execution modules. See
[`AGENT_STRATEGY_PIPELINE.md`](AGENT_STRATEGY_PIPELINE.md).

The `deployment` control plane is also effect-isolated from trading. It verifies
content-addressed releases and offline wheelhouses, manages separate promotion
channels, performs atomic version/rollback state changes, rotates narrowly scoped
credential files, archives mutable state and publishes health/alert artifacts. It has
no exchange, execution, risk or notification dependency and never starts a service.
See [`DEPLOYMENT.md`](DEPLOYMENT.md).

The planned investor-accounting boundary is separate from execution. Confirmed
owned fills, commissions and funding may enter it as versioned idempotent
events, but accounting and reporting receive no order capability. Investor
balances come from an append-only double-entry ledger and reconciled valuation
snapshots, never directly from wallet deltas. See
[`INVESTOR_LEDGER_AND_REPORTING.md`](INVESTOR_LEDGER_AND_REPORTING.md).
The implemented offline MVP and frozen accounting semantics are documented in
[`ACCOUNTING_POLICY.md`](ACCOUNTING_POLICY.md); operational boundaries are in
[`INVESTOR_LEDGER_OPERATIONS.md`](INVESTOR_LEDGER_OPERATIONS.md).
