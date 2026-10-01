# Offline testing safety

The default test suite must not reach Binance, SMTP or any other network service and
must never be run with live credentials.

The sandbox acceptance tests require Linux with unprivileged user and network
namespaces, `bubblewrap`, and `unshare`. Missing or disabled isolation is a test
failure; the suite does not silently skip this boundary.

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

The automatic fixture removes credential and live environment variables, changes each
test into a fresh temporary directory, blocks socket and SMTP construction, and rejects
creation of runtime directories. Tests use injected fakes rather than real SDK clients.

The default suite must not load `.env`, query account state, place/cancel orders, change
leverage or account mode, send email, read ignored runtime state or depend on local
datasets. Testnet and live checks are separate operator-approved activities and must
never be hidden behind a pytest marker or default command.

Every trading-safety fix should cover the normal result, explicit rejection, unknown or
timeout result, and proof that no duplicate external mutation occurs.
