# Strategy interface and package verification

A standard strategy is pure policy. It receives typed, immutable snapshots and
may return a `StrategySignal`; it cannot receive credentials, an exchange
adapter, an execution coordinator, a risk engine, a supervisor, or writable
runtime paths.

A candidate generated source file is treated as inert bytes. Validation parses
its AST without importing or executing it. Imports are allowlisted to immutable
domain types, the signal interface, and selected standard-library value types.
Environment, file, network, process, exchange, execution, risk, configuration,
notification, dynamic import, and introspection capabilities are rejected.

The package manifest binds:

- exact source SHA-256;
- strategy identity and interface version;
- parameter fingerprint.

A run approval additionally binds the manifest fingerprint, commit,
environment fingerprint, risk-policy fingerprint, and validity window.
Any change requires a new approval. Validation reports are deterministic,
schema-versioned JSON.

This legacy Python-source validator remains a preflight control, not a security
sandbox, and generated Python is still never executed. The completed Agent pipeline
uses declarative `StrategySpec` data, a trusted deterministic interpreter and an
OS-isolated worker with resource limits. It is offline-only and has no Testnet/live
entry point. See `AGENT_STRATEGY_PIPELINE.md`.
