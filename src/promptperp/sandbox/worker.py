from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

from promptperp.signal_engine import (  # noqa: E402
    EngineState,
    MarketEvent,
    SignalEngine,
)
from promptperp.strategy_spec import canonical_json, load_strategy_spec  # noqa: E402

_MAX_REQUEST = 1024 * 1024


def _main() -> int:
    request_path = Path("/input/request.json")
    payload = request_path.read_bytes()
    if len(payload) > _MAX_REQUEST:
        raise RuntimeError("request exceeds worker limit")
    request = json.loads(payload)
    if (
        set(request) != {"schema_version", "spec", "events", "state"}
        or request["schema_version"] != 1
    ):
        raise RuntimeError("worker protocol is invalid")
    spec = load_strategy_spec(canonical_json(request["spec"]))
    raw_state = request["state"]
    if not isinstance(raw_state, dict):
        raise RuntimeError("worker checkpoint is invalid")
    state = EngineState.from_mapping(raw_state)
    engine = SignalEngine(spec, state)
    proposals: list[dict[str, Any]] = []
    events = request["events"]
    if not isinstance(events, list) or len(events) > 10000:
        raise RuntimeError("worker event batch is invalid")
    for raw_event in events:
        if not isinstance(raw_event, dict):
            raise RuntimeError("worker event is invalid")
        event = MarketEvent.from_mapping(raw_event)
        emitted, _ = engine.process(event, accepted_at=event.observed_at)
        proposals.extend(proposal.to_dict() for proposal in emitted)
    response: dict[str, Any] = {
        "schema_version": 1,
        "status": "OK",
        "proposals": proposals,
        "state": engine.state.to_dict(),
    }
    sys.stdout.buffer.write(canonical_json(response))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:
        response = {
            "schema_version": 1,
            "status": "REJECTED",
            "error_type": type(exc).__name__,
        }
        sys.stdout.buffer.write(canonical_json(response))
        raise SystemExit(2)
