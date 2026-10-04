# Shared account execution and equity reports

**English** | [简体中文](SHARED_EXECUTION.zh-CN.md)

These library components help integrators keep strategy ownership separate when an
exchange combines positions. They do not provide a live worker, a trading strategy,
or account isolation. The beginner quick start remains offline.

## What the components do

- `VirtualPositionStore` records strategy and run ownership, quantities, entry costs,
  realized PnL, fees and funding. Decimal arithmetic and persisted event identities
  support deterministic replay. Decimal zero quantities are treated as flat.
- `VirtualFillRouteRegistry` binds a client order to one owner and indexes confirmed
  orders by symbol and exchange order ID. Ambiguous lookups are rejected.
- `VirtualFillRecovery` validates ownership for a batch before applying fills.
  Unknown fills block recovery; they are not assigned to a convenient strategy.
- `BasketPolicy` and `preflight_basket` validate a balanced target basket, rounding,
  minimum quantities and notional limits. Position count and target weight are
  mandatory caller inputs; the package supplies no production portfolio settings.
- `BasketExecutionStateMachine`, its coordinator and rollback manager persist
  reductions before additions, order uncertainty, partial fills and owned rollback.
  An interrupted basket is not assumed to have completed or remained empty.
- `SharedAccountTurn` serializes cooperating local workers. `SharedAccountBusy`
  means no lock was acquired: a worker may defer its poll without advancing a
  signal or submitting an order. An unresolved execution barrier or failed
  reconciliation is a different error and must remain blocking.

The existing platform's exclusive symbol-allocation policy is unchanged. Merely
installing this release does not enable overlapping positions. A downstream worker
must explicitly integrate ownership projection, actual exchange reconciliation,
shared locking, approvals and crash recovery before using these components live.
The file lock coordinates one host, not distributed workers.

Run `python examples/offline_shared_execution.py` for a synthetic four-leg preflight.
It constructs no exchange client and prints `execution_permitted: false`. Its numbers
are test inputs, not portfolio recommendations.

## Exchange reads and failures

`BinanceAccountSnapshotReader` reads positions, balance, position mode and both
ordinary and conditional open orders through an injected exchange client.
`BinanceAccountEventReader.list_fills` provides normalized timestamped fills.
These calls are private account reads, not offline demonstrations.

`read_api_permissions` checks permission flags; its default network transport
requires the runtime external-effect and approval gates. Tests inject a fake
transport. `require_restricted_futures_scope` requires reading, futures trading and
IP restriction, and rejects withdrawal or transfer permissions. It never modifies
API permissions. Integrators must separately validate the intended account and mode.

Only narrowly recognized non-execution rejections are classified as definitive.
Unrecognized errors, timeouts and rejected responses containing fills remain
uncertain and require reconciliation, not blind retries.

## Equity history and scheduled summaries

`reporting.equity_history.EquityHistory` accepts caller-supplied, reconciled strategy
valuations and stores one sample per UTC minute in an owner-private SQLite file.
Supply an explicit positive initial equity, deployment identity, timezone and daily
hour. The persisted policy cannot silently change when reopening that deployment.

`summary(start, end)` calculates sampled interval PnL, inception return and sampled
maximum drawdown. `html(start, end)` returns an offline HTML/SVG equity curve. Both
require timezone-aware bounds and disclose the actual sample coverage. Gaps over
two minutes are not connected. Samples must use one consistent valuation method.
This initial version assumes no deposits or withdrawals during a deployment; it is
not a cash-flow-adjusted fund performance engine and is not investor pool NAV.

`daily(now, notify)` calls an explicitly supplied delivery callback after the
configured local hour, only when a recent sample exists. It has no built-in SMTP
client or background scheduler. Run it outside the execution critical path.
Delivery is claimed before invoking the callback: uncertain delivery or a crash
requires review, not automatic resending. This prevents blind duplicates but cannot
guarantee exactly-once delivery. Keep report data and generated reports out of Git.

## Migration and safety checks

Existing single-intent execution and investor accounting APIs are unchanged. Start
new virtual-position and routing stores for a new integration; no automatic legacy
database migration, position adoption or account-mode change is provided. With
existing positions, retain the old worker and perform a separately reviewed,
owned-state migration instead of deleting state.

Run `./scripts/quality.sh` before integration. All new fixtures use synthetic data
and fake exchange clients; tests never grant live authority. Shared accounting does
not isolate exchange liquidation, margin, outages or manual account actions.
