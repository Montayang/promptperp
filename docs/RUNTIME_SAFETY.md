# Runtime safety and external-effect approval

All runtime configuration defaults to `OFFLINE`. Importing a module, creating an
adapter or changing local control-plane state does not authorize network or account
mutation.

## Runtime modes

- `OFFLINE`: external effects are blocked.
- `TESTNET`: requires an explicit external-effects flag plus run-specific approval.
- `LIVE`: requires the flag and a valid `LiveRunApproval` bound to the exact strategy,
  run, commit, symbols, sides, margin, notional, leverage and time window.

Configuration reads environment variables only through an explicit call. It never
searches for `.env` during import. Error messages report failed rules without printing
credential values.

## Adapter behavior

The Binance adapters receive SDK ports through dependency injection. They do not load
credentials or access the network during construction. Every mutation checks runtime
authorization before an SDK call. Timeouts and malformed responses become unknown
states, never empty positions or successful orders.

The public distribution exposes these adapters as library boundaries but ships no live
strategy worker. Downstream applications must construct approvals from operator-owned
configuration, perform authoritative preflight reconciliation, hold an exclusive run
lease and use the risk and execution state machines.

## Generated strategies

Agent output cannot construct runtime configuration, read credentials or call an
adapter. The public Agent pipeline ends with `execution_permitted=false`; connecting a
reviewed strategy to a live worker is a separate operator-controlled integration.
