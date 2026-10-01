# B1-B8 controlled Agent pipeline acceptance

Date: 2026-10-01 (Asia/Singapore)

Scope: deterministic offline candidate generation and execution only. No exchange,
private API, email, Testnet, live strategy, account state or customer data was used.

## Acceptance results

| Gate | Evidence | Result |
|---|---|---|
| B1 StrategySpec | canonical bytes/hash, exact schemas, v0 migration, unknown-version and forbidden-capability rejection | PASS |
| B2 DSL | fixed operators/indicators, Decimal reducer, ordered input checks, explicit checkpoint and authority-free proposal | PASS |
| B3 evaluation | five scenario classes, deterministic replay, fixture/spec/interpreter binding and fail-closed promotion | PASS |
| B4 bundle | exact-file manifest, content hashes, provenance validity/trust checks, symlink/extra/change rejection | PASS |
| B5 approvals | append-only hash chain, transaction lock, operator capability, expiry/revoke/deny and single consumption | PASS |
| B6 sandbox | bubblewrap plus user/network namespaces, minimal mounts, empty environment/temp, resource limits and no fallback | PASS on this host |
| B7 orchestration | binding verification, checkpoint/recovery, deduplication, allocation/risk transformation and safe stop | PASS |
| B8 red team | malicious fields, inert prompt text, bundle tampering, forged approval, policy changes, replay and unavailable sandbox | PASS |

The end-to-end example starts with a canonical candidate, evaluates it, builds and
verifies a bundle, creates an approval request, simulates a distinct operator grant,
consumes it once, runs the trusted interpreter in the Linux sandbox, checkpoints the
result and produces an offline risk request with execution explicitly disabled.

## Red-team decisions

- Prompt text has no instruction channel in the schema and remains inert text.
- Arbitrary generated Python is not accepted or executed.
- Specs cannot set account, margin, leverage, order type or execution authority.
- An invalid operator capability cannot grant, revoke or alter approval.
- A changed bundle, environment, allocation policy or risk policy invalidates grant.
- A consumed approval cannot start another run.
- Worker failure, malformed output, excessive input/output or absent isolation blocks.
- Only the trusted declarative interpreter is staged into the mount namespace.

## Residual risk and decision

Linux sandbox acceptance is host-specific and must repeat after kernel, bubblewrap,
Python or deployment changes. HMAC provenance assumes the deployment phase protects
the external trust root and operator capability under a privileged OS identity. No
Testnet/live protocol has been evaluated.

Decision: B1-B8 are accepted for offline candidate production. Testnet and live
promotion remain prohibited and require a separately approved future stage.
