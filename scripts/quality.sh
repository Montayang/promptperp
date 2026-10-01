#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

python -m ruff format --check src tests examples scripts
python -m ruff check src tests examples scripts
python -m mypy \
  src/promptperp/accounting \
  src/promptperp/config \
  src/promptperp/domain \
  src/promptperp/deployment \
  src/promptperp/exchange \
  src/promptperp/execution \
  src/promptperp/operations \
  src/promptperp/risk \
  src/promptperp/reporting \
  src/promptperp/runtime \
  src/promptperp/agent_pipeline \
  src/promptperp/approvals \
  src/promptperp/evaluation \
  src/promptperp/notifications \
  src/promptperp/sandbox \
  src/promptperp/signal_engine \
  src/promptperp/storage \
  src/promptperp/strategies \
  src/promptperp/strategy_spec \
  src/promptperp/strategy_packages
python -m pytest
python examples/offline_accounting.py
python examples/offline_platform.py
if python -c 'from promptperp.sandbox import SandboxRunner; raise SystemExit(0 if SandboxRunner().probe().available else 1)'; then
  python examples/offline_agent_pipeline.py
else
  echo "SKIP: offline Agent example requires host sandbox acceptance"
fi
python examples/offline_deployment_drill.py
python scripts/scan_repository.py
python -m pip check
python -m build --no-isolation
python scripts/inspect_artifacts.py
