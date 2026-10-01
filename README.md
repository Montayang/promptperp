# PromptPerp

AI-native, safety-gated infrastructure for Binance USDⓈ-M Futures strategies.

PromptPerp turns a structured strategy idea into a reproducible candidate package,
evaluates it offline, binds it to an explicit operator approval, and routes approved
intent through portfolio, risk, ownership and recovery controls. The repository is an
independent open-source project and is not affiliated with or endorsed by Binance.

> [!WARNING]
> Perpetual futures can cause rapid and total loss of capital. PromptPerp is
> experimental software, not investment advice, and has not received an independent
> security audit. Nothing in this repository authorizes a live run.

## What is included

- A versioned `StrategySpec` contract and deterministic Decimal-based interpreter.
- Offline evaluation, content-addressed strategy bundles and tamper detection.
- A one-use, expiring operator-approval ledger.
- OS-level sandbox execution with no in-process fallback.
- A multi-strategy control plane with capital budgets and symbol ownership.
- Fail-closed Binance USDⓈ-M adapters, execution recovery and protection ownership.
- Portfolio and per-intent risk gates with kill-switch/degraded modes.
- An optional shared-account investor ledger and reporting subsystem.
- Offline deployment, promotion, backup, rollback and health primitives.
- One deliberately simple example strategy: `threshold_momentum`.

Private production strategies are not part of this repository. The public package does
not bundle a live strategy service or an autonomous LLM. An external AI agent may turn
natural-language requirements into the documented `StrategySpec`; PromptPerp treats
that output as untrusted data and never grants it trading authority.

## Safety model

```text
natural-language idea
        ↓ external AI agent
untrusted StrategySpec
        ↓ schema + semantic validation
offline evaluation + sandbox
        ↓ immutable bundle
explicit, expiring, one-use approval
        ↓ allocation + portfolio + risk gates
owned execution state machine
        ↓ deployment-specific live integration
Binance USDⓈ-M Futures
```

The public quick start stops before the approval and exchange layers. Live operations
require a deployment-specific worker, private credentials, authoritative account
reconciliation and a separate operator decision. Order uncertainty, stale data,
foreign positions, ownership ambiguity and failed protection all block new risk.

## Offline quick start

Use Python 3.12 for the tested development environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

Run only the bundled offline demonstrations:

```bash
python examples/offline_agent_pipeline.py
python examples/offline_platform.py
```

Neither command reads `.env`, connects to Binance, sends email or permits execution.

## Project status

PromptPerp is an alpha release. The public repository provides the engine and safety
boundaries, not a turnkey profit system.

| Capability | Public status |
|---|---|
| StrategySpec and deterministic signal engine | Offline supported |
| AI candidate validation and packaging | Offline supported |
| Operator approval ledger | Offline supported |
| Example strategy | Offline only |
| Binance adapters and recoverable execution | Library API, integration required |
| Generic live strategy daemon | Not included |
| Autonomous AI approval or trading | Explicitly prohibited |

Non-sensitive results from two bounded real protocol cycles are retained in
[the A9 acceptance record](docs/acceptance/A9_REAL_PROTOCOL_ACCEPTANCE.md). They are
compatibility evidence, not a performance claim or a recommendation to trade.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Agent strategy pipeline](docs/AGENT_STRATEGY_PIPELINE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Strategy interface](docs/STRATEGY_INTERFACE.md)
- [Runtime safety](docs/RUNTIME_SAFETY.md)
- [Risk engine](docs/RISK_ENGINE.md)
- [Execution recovery](docs/EXECUTION_RECOVERY.md)
- [Exchange adapters](docs/EXCHANGE_ADAPTERS.md)
- [Multi-strategy platform](docs/MULTI_STRATEGY_PLATFORM.md)
- [Testing safety](docs/TESTING_SAFETY.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Security policy](SECURITY.md)

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a
pull request. Tests must remain offline and credential-free. Security vulnerabilities
must be reported privately as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
