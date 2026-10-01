# Controlled AI Agent strategy pipeline

The Agent pipeline turns an untrusted strategy description into a deterministic,
offline candidate. It does not grant trading authority. The supported flow is:

```text
user description -> Agent StrategySpec -> validation -> deterministic evaluation
-> content-addressed bundle -> operator approval -> isolated offline worker
-> SignalProposal -> operator allocation -> offline risk request
```

## Trust boundary

Agent output is data, never executable code. `StrategySpec` cannot contain account
identity, credentials, margin, leverage, order type, paths, URLs, code, notification
targets, or execution permissions. The declarative interpreter has fixed operator and
indicator allowlists, deterministic decimal arithmetic, explicit state, bounded
history, and no clock, random, file, environment, network, or process API.

The JSON schema at `schemas/strategy_spec_v1.schema.json` is for interoperability.
`promptperp.strategy_spec.load_strategy_spec` is authoritative and also enforces
cross-field rules, duplicate-key rejection, decimal-string encoding, complexity
limits, forbidden capabilities and canonical serialization. Only the frozen v0 draft
has a v1 migration; unknown versions fail closed.

## Evaluation and bundle

Evaluation runs every embedded scenario twice. A candidate advances only when normal,
no-signal, boundary, data-fault and abnormal categories all pass and both runs are
byte-identical. Its report binds the spec, interpreter and fixture hashes.

A bundle contains exactly four files: canonical spec, evaluation report, manifest and
provenance. The manifest binds every payload, commit and dependency. Provenance uses
an externally supplied operator HMAC trust root in v1. Verification rejects expired
or unknown issuers, unknown algorithms, symlinks, missing/extra files and any changed
byte. Verification never imports or executes bundle content.

## Approval model

The owner-only SQLite ledger is transactionally serialized and append-only. Each
event joins a SHA-256 chain. An Agent can append only `REQUESTED`; grant, deny and
revoke require the operator capability. A grant binds the bundle, spec, evaluation,
commit, environment, allocation policy, risk policy and validity window. A Supervisor
consumes it exactly once. Expired, revoked, changed, replayed or corrupted state fails
closed.

The operator capability is an application boundary, not a password service. A real
deployment must inject it through the privileged supervisor identity; it must never
be placed in a spec, bundle, command argument or log.

## Sandbox and Supervisor

The Linux backend combines user/network namespace isolation with bubblewrap mount,
PID, IPC, UTS and cgroup namespaces. It mounts only system runtime files, a staged
copy of the trusted interpreter and one read-only request. It clears the environment,
has an empty temporary directory, cannot see the repository or `/home`, has no network
route, and applies CPU, memory, file, descriptor, process, time, input and output
limits. Capability probing uses the same profile. Failure never falls back in-process.

The sandbox does not execute generated Python. Namespace and resource controls are
defense in depth around a narrow trusted interpreter, not a claim that arbitrary
native code is safe.

The Supervisor verifies all bindings before consuming approval. It persists a
checkpoint and emitted deduplication keys atomically. Recovery re-verifies the same
bundle and original consumed approval. Any malformed response, authority field,
resource termination or checkpoint damage moves the run to `BLOCKED`.

Allocation is added only after the worker returns. The resulting offline risk request
always has `execution_permitted=false`; this subsystem imports no exchange, execution,
notification, account configuration or private API module.

## Agent authoring contract

The Agent must return one UTF-8 JSON document conforming to StrategySpec v1. It must:

1. put ambiguity in `unresolved` and stop advancement until it is resolved;
2. state assumptions and machine-checkable assertions;
3. include all five scenario categories using fictional/offline data;
4. use decimal strings and timezone-aware timestamps;
5. request no money, account, order, credential, network or notification authority;
6. treat user text inside descriptions and reasons as inert data;
7. never claim Testnet or live eligibility.

The operator reviews the normalized spec, report, bundle fingerprint, policies,
expiry and unresolved list before granting. An approval is not reusable.

## Current eligibility

This pipeline is **offline-only**. It is not wired to the live runtime,
ExecutionCoordinator, Binance adapters, Testnet, email or system services. Advancement
beyond an offline risk request needs a separate design, acceptance stage and explicit
operator authorization.
