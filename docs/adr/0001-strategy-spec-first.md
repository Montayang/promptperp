# ADR 0001: StrategySpec-first Agent integration

Status: accepted for the next development stage
Date: 2026-09-02

## Context

Executing Agent-generated Python in the live-system process would expose
credentials, runtime state, exchange adapters, risk internals, and host
capabilities. Static AST validation is useful preflight but cannot confine
arbitrary Python.

The desired workflow still needs broad strategy expression, deterministic
review, reproducible packages, and an operator approval boundary.

## Decision

The default Agent artifact is a declarative, versioned JSON `StrategySpec`.
A trusted interpreter evaluates its bounded DSL and produces a
`SignalProposal`. Actual sizing is supplied by an operator-owned allocation
policy and remains subject to the risk engine.

Arbitrary Python is not the primary strategy format. A future advanced plugin
may run only through a fail-closed OS sandbox with no credentials or network,
read-only inputs, narrow IPC, and resource limits. Lack of sandbox capability
is a hard refusal, never an in-process fallback.

## Consequences

Benefits:

- deterministic canonicalization and review;
- substantially smaller attack surface;
- easier property testing and machine-readable reports;
- strategy logic cannot directly claim account authority.

Costs:

- the initial DSL cannot express every Python strategy;
- indicators and state transitions require explicit versioned primitives;
- advanced strategies need a separate sandboxed extension path.

This decision does not authorize generated strategy execution or live trading.
