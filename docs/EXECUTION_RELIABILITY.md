# Execution reliability: lessons and integration contract

**English** | [简体中文](EXECUTION_RELIABILITY.zh-CN.md)

This is a generalized engineering summary, not a live incident log. Examples and
tests use synthetic data. No proprietary strategy, account history, credentials,
production thresholds or deployment settings are included. These components are
library APIs, not a ready-to-run trading service or permission to trade.

After installing the development dependencies, run the credential-free wiring
example with `python examples/offline_execution_recovery.py`. It uses temporary
state and synthetic callbacks only; a production audit must be durable.

## Issues addressed

| Failure pattern | Public remedy | Regression coverage |
|---|---|---|
| FILLED acknowledgement arrives before execution details / query indexes | The futures adapter queries the acknowledged order, then its deterministic client ID. At most seven reads; never a second placement. | `test_filled_order_confirmation.py`: delayed visibility, exhaustion, wrong identity, rate limits, transport errors |
| SDK rejection fields change across transport versions | Definitive rejection requires an allowlisted code/message pair, including current numeric exchange codes. Unknown errors still require reconciliation. | Current numeric codes and legacy HTTP-400 shapes are tested. |
| Trade history and physical positions converge at different times | `FillConfirmation` permits bounded read-only convergence only inside the exact quantity transition proved by fills. | `test_fill_confirmation.py`: increases, reductions, full exits, overfills, foreign symbols/sides |
| A first refresh clears dirty symbols while history still lags | Confirmation retains an immutable affected-symbol set and supplies it on every retry. | Same refresh scope remains present until exact confirmation. |
| Final repair step exhausts API capacity after an order has already executed | `RequestBudget` reserves the whole operation before the account lock; include final settlement and the second account snapshot. | `test_request_budget.py`: complete cost envelope, contention, restart, expiry, policy mismatch |
| Split settlement batches cause premature permanent faults | `SettlementGate` persists a bounded no-trading hold only for typed `ValuationPending`; unknown ownership/orders remain immediate faults. | `test_settlement_gate.py`: restart deadline, full verification, permanent faults, clock rollback |
| Shared-account ownership and uncertain writes are confused with retryable transport errors | Existing virtual routes and basket state keep deterministic order identities and require recovery by query. | Basket coordinator, virtual route, recovery and account-turn tests |

## Integration order

1. Compute a conservative request-weight envelope. Include preflight, all possible
   confirmation reads, recovery reads, final full settlement and a second account
   snapshot. Weights/limits must be reviewed for the actual endpoints and shared IP.
2. Call `RequestBudget.reserve(weight)` **before** `SharedAccountTurn.acquire()`.
   If unavailable, defer outside the lock, keep the supervisor heartbeat fresh and
   apply an operator-approved overall timeout. Do not spin or sleep holding the lock.
3. Under the account lock, consume permit weight immediately before every request,
   including read retries. Configure finite transport timeouts and disable automatic
   mutation retries. Reconcile the complete account and verify ownership, approvals,
   stops and protection before any new risk.
4. Persist intent and fill routing before placement. An uncertain placement remains
   uncertain: query its original identity, never place a replacement based on a timeout.
5. Derive `FillConfirmation.confirm(before=..., expected=...)` quantities from
   **confirmed cumulative fills**, not desired targets. Its refresh callback must
   query every supplied symbol even if another component cleared a dirty set.
   Persist every `ConfirmationAttempt` through the required audit callback.
6. Reconcile settlement with `SettlementGate.reconcile(full_read_only_check)`.
   Raise `ValuationPending` only after orders and ownership have been verified and
   the remaining mismatch is specifically settlement valuation. While pending,
   the gate raises and blocks trading; it does not forgive the amount difference.
   Only a successful full check clears this temporary hold. It never clears an
   existing fault or operator stop, and success is not execution authorization.

These steps require application wiring. The basket coordinator does not silently
enable these new gates, and none of these APIs starts a process or changes an account.
Pending settlement is not a substitute for emergency-risk handling: that policy must
be approved separately and cannot assume unverified quantities or send blind orders.

## Bounds and persistence

- Incomplete FILLED details allow seven query attempts and 14.5 seconds of scheduled
  backoff in total. Each network read also needs a caller-configured timeout.
  Only explicit `-2013` missing-order index results are retried; other errors stay
  unknown. Every recovered response must match symbol, exchange order ID, client ID,
  side, position side and requested quantity, with exact FILLED quantity and price.
- Position confirmation defaults to three observations within a five-second retry
  admission window. Callback I/O is not forcibly interrupted. Extra positions,
  unrelated changes, duplicate rows and quantities outside the confirmed transition
  fail immediately; no rounding tolerance hides ownership mismatches.
- The request budget uses one local SQLite database per shared limit scope and pins
  capacity/window policy across restarts. Reserve a whole turn; consume from the
  process-local permit. A permit lasts one window, while its reservation lasts two
  windows to conservatively account for late consumption. Unused capacity is not
  refunded, so throughput may be lower than the exchange maximum. An expired permit
  cannot make requests. A restarted process must obtain a new permit and reconcile.
  All relevant workers must participate; this is not multi-host coordination.
- Settlement state uses a separate local SQLite database. The caller supplies the
  maximum wait; it is pinned in persisted state. The first pending timestamp survives
  restart. Expiry, backward clock movement and unrelated errors persist a fault.
  Use stable state paths and restrictive OS permissions. Never delete or recreate
  these databases to bypass a fault; recovery requires reviewed exchange evidence.

## Lessons that are not universal strategy defaults

- Forced liquidation must be reconciled using actual fills, fees and position
  ownership; it is not automatically permission to reopen or flatten every strategy.
  Leverage, isolated margin, cooldown, continuation and loss limits remain explicit
  strategy/account policies. This update does not add an automatic liquidation worker.
- Coarse quantity steps can make a nominally neutral target unbalanced or infeasible.
  Existing basket preflight must reject infeasible baskets before adding risk; do not
  quietly increase per-leg weights. Repair thresholds and acceptable drift are policy.
- Restart recovery must use exchange truth and durable order identities. A genuinely
  new repair needs a new persisted basket identity; resuming one reuses its identity.
  Do not derive IDs solely from counters that reset when a runtime directory changes.
- Long-running market ingestion needs bounded queries, retention, freshness checks,
  and memory monitoring. There is no public production market-ingestion worker to
  patch here; do not infer one from these execution helpers.
- Local success is not live acceptance. Run the offline quality gate, then review
  the account-specific integration and obtain separate execution approval. No live
  orders or email are sent by the regression suite.
