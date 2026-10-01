# Entry-point support status

The public package separates offline control commands from effectful integrations.

## Offline-supported commands

- `promptperp-platform` validates plugins and parameters, creates local plans,
  changes local lifecycle state, reconciles supplied snapshots and emits reports.
  Its `start` subcommand does not launch a process or place an order.
- `promptperp-deploy` verifies artifacts, manages local promotion state, performs
  offline installation and creates/restores state archives. It never starts a
  service.
- `examples/offline_agent_pipeline.py`, `examples/offline_platform.py`,
  `examples/offline_accounting.py` and `examples/offline_deployment_drill.py` use
  temporary fictional state and no network.

## Example strategy

`promptperp.strategies.threshold_momentum` is the only bundled strategy. It exists to
demonstrate the plugin contract and is offline-only. It is not approved for Testnet or
live trading and is not presented as profitable.

## Effectful library boundaries

`promptperp.exchange` contains dependency-injected Binance adapters. Importing or
constructing them does not authorize external effects. The public repository has no
generic live strategy daemon, no private credentials and no systemd strategy unit.

The email provider commands are disabled unless invoked with `--execute`, an exact
clean commit and an approval window of at most one hour. Their presence does not
authorize sending mail.

## Agent pipeline

The Agent demonstration validates a declarative spec, evaluates it, verifies a bundle,
consumes a one-use approval and invokes a sandboxed interpreter. Its final request
records `execution_permitted=false`; generated output cannot call exchange, execution,
risk, accounting or notification modules.

## Default rule

Anything not explicitly documented as offline-supported must be treated as an
integration API, not a runnable example. A downstream live worker needs its own
documented approval, risk limits, ownership, reconciliation, recovery and stop model.
