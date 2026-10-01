# ADR 0002: Shared-account investor ledger

Status: accepted for development preparation
Date: 2026-09-20

## Context

Multiple investors will share one Binance futures account because exchange subaccounts are not available. Exchange wallet balance and net positions therefore cannot identify each investor's economic ownership. The system also needs scheduled statements and read-only email queries without exposing trading authority.

The first version may assign each investor fully to one strategy. Deposits and withdrawals are not allowed while any position or unsettled trading state exists. Fees and performance compensation are deferred.

## Decision

Create a bounded investor-accounting and reporting subsystem with these properties:

- an append-only, transactional double-entry ledger is the source of investor balances;
- capital is unitized in strategy pools, with one active 100% subscription per investor in the first version;
- confirmed owned fills, commissions and funding connect the execution ledger to accounting through versioned idempotent events;
- exchange wallet deltas are reconciliation evidence, not profit attribution;
- any managed or foreign position, open order, unsettled event, stale reconciliation or unexplained difference blocks capital flows;
- statements derive from immutable reconciled valuation snapshots;
- scheduled email supports daily, weekly and monthly policies;
- inbound email is a separately privileged, command-whitelisted, read-only interface;
- accounting can expose reconciled allocatable equity, but it has no exchange execution capability and cannot grant live approval.

Production storage for the first version will be embedded SQLite behind repository interfaces. It runs on one host with one accounting service as the sole writer; accounting, valuation, scheduled-statement and outbox mutations are serialized inside that process. The separate email-query process receives read-only access or immutable published snapshots. Foreign keys, WAL, synchronous durability, transactions and online backup are explicit configuration and acceptance requirements. JSON files are not an acceptable production ledger.

## Consequences

Benefits:

- investor equity is reproducible and auditable independently of email or runtime files;
- deposits are not mistaken for returns and one strategy's result is not silently assigned to another;
- reporting and queries can operate without Binance or trading credentials;
- the model can later add multiple strategy allocations without rewriting history.

Costs and limitations:

- a shared exchange account still has counterparty, margin and operational contagion across investors;
- exact execution ownership and fee/funding attribution are prerequisites, not problems accounting can solve after the fact;
- cash flows may be delayed until a globally flat, fully reconciled window;
- running the subsystem requires durable database operations, backups, privacy controls and reconciliation monitoring;
- SQLite is not shared over a network filesystem and does not provide multi-host writers or active-active availability; those requirements trigger a new storage ADR and likely PostgreSQL migration.

## Alternatives rejected

- **Wallet-delta allocation:** cannot distinguish trading PnL, fees, funding, deposits, withdrawals or foreign activity.
- **One JSON balance file per investor:** lacks atomic cross-investor transactions, constraints, concurrency control and reliable audit recovery.
- **A PostgreSQL service in the first version:** adds service deployment, credentials, networking, upgrades and backup operations without a current concurrency or availability requirement.
- **Email as an administrative channel:** a compromised mailbox could mutate funds or trading state; read-only scope sharply limits impact.
- **Immediate multi-strategy percentages:** adds rebalancing and cross-pool allocation before the base accounting and reconciliation invariants are proven.
- **Accounting-only synthetic isolation:** internal entries cannot make exchange-netted positions or shared margin technically isolated.
