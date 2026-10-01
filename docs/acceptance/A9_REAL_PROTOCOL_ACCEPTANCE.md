# A9: standard execution real protocol acceptance

Status: accepted on Binance mainnet on 2026-09-30 (Asia/Singapore). The
bounded run is complete and no acceptance strategy remains running.

## Offline deliverable

- A separate one-shot acceptance harness produced an immediate bounded intent
  without depending on a production strategy signal.
- It uses the production `RiskEngine`, `ExecutionRegistry`,
  `ExecutionCoordinator`, Binance ordinary-order and protection adapters,
  append-only ownership store, real-event reader, exact settlement and investor
  ledger reconciliation.
- It has hard limits for margin, leverage, cycle count, protection distance,
  observation duration and total accepted loss.
- A fresh run requires a flat, order-free account. Resume requires the same run
  ID and immutable parameters.
- Offline tests cover limit validation, environment binding, approval binding,
  protection confirmation followed by controlled close, protection-trigger
  recovery, settlement event completeness and post-settlement loss checks.

## Real acceptance checklist

- [x] Operator approves environment, symbol, margin, leverage, stop/take
  ratios, cycle count, observation duration, maximum total loss and expiry.
- [x] Read-only preflight confirms hedge mode, no positions, no open orders,
  sufficient balance, ledger consistency and investor-equity reconciliation.
- [x] At least two complete cycles reach `SETTLED` without unexplained or
  quarantined events.
- [x] Entry and both protection identities are confirmed by exchange queries.
- [x] A controlled close and, when market movement permits, a protection close
  are recovered to actual trade order IDs.
- [x] Remaining protection is cleaned by its owned client identity only.
- [x] Realized PnL, commission and any funding are attributed to each intent;
  zero funding is recorded as zero rather than inferred.
- [x] Exact settlement agrees with wallet change and investor-ledger posting.
- [x] Final account is flat, has no acceptance orders and the loss cap is not
  exceeded.

## Non-sensitive real evidence

- Environment and bounds: Binance mainnet, `ETHUSDT`, 25 USDT initial margin,
  5x leverage, 0.1% stop loss, 0.1% take profit, no email, and a 3 USDT
  aggregate loss cap. Approval expired at 2026-09-30 22:00 Asia/Singapore.
- The first real cycle exposed a Binance `FILLED` response whose final fill
  fields were not yet populated. It was immediately safety-closed, recovered
  through the append-only execution ownership records, and reached `SETTLED`
  with two attributed trade events and net change `-0.30849877` USDT.
- Commit `35aaa9b` makes a known market order reconcile its fill fields by
  exchange order ID and preserves a recoverable unknown state when Binance has
  not made the order query-visible yet.
- The remaining approved cycle ran from commit `06bd3a2`. Its entry and both
  protection orders were confirmed by exchange queries. A protection order
  closed the position, the remaining owned protection was cleaned, and the
  execution reached `SETTLED` with two attributed trade events and net change
  `-0.25085686` USDT.
- Commit `06bd3a2` gives every acceptance process attempt a retry-safe
  reconciliation identity. This was required when the same run resumed after
  an exchange-consistency delay.
- Aggregate net change was `-0.55935563` USDT, within the approved 3 USDT cap.
  Both settlements were posted to the current investor allocation and each
  final investor-equity reconciliation passed. No event was quarantined.
- The final private-account postflight confirmed no position and no normal or
  algorithm acceptance order. Runtime reports and account data remain in the
  ignored local runtime directory and are not committed.
