# Contributing

PromptPerp accepts security, reliability, documentation and strategy-interface
improvements. A contribution must not include proprietary strategies, credentials,
account data, investor data, logs, runtime state, datasets or server details.

## Development setup

The complete safety suite requires Linux with unprivileged user and network
namespaces enabled, plus `bubblewrap` and `unshare`. On Debian or Ubuntu, install
the tools with:

```bash
sudo apt-get install bubblewrap util-linux
```

Use Python 3.12 and work on a topic branch:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip install --no-deps -e .
./scripts/quality.sh
```

All tests and examples must be offline. Never use a live entry point as an installation
check. Changes to execution, risk, approval, ownership or recovery need success,
rejection, uncertain-outcome and tamper tests plus a user-visible migration note.

Generated strategy material must use `StrategySpec`; pull requests that execute
generated Python or bypass operator approval will not be accepted.

Before committing, review `git status`, `git diff`, `git diff --check`, the repository
scan and built artifacts. Do not force-push shared branches.
