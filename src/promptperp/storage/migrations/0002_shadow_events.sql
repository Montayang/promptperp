CREATE TABLE IF NOT EXISTS execution_intents (
    intent_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (run_id, intent_id)
);

CREATE INDEX IF NOT EXISTS execution_intents_symbol_time_idx
ON execution_intents(symbol, opened_at, closed_at);

CREATE TABLE IF NOT EXISTS owned_exchange_orders (
    client_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES execution_intents(intent_id),
    role TEXT NOT NULL CHECK (role IN ('ENTRY', 'STOP', 'TAKE_PROFIT', 'EXIT')),
    is_algo INTEGER NOT NULL CHECK (is_algo IN (0, 1)),
    algo_id TEXT UNIQUE,
    exchange_order_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS owned_exchange_orders_intent_idx
ON owned_exchange_orders(intent_id);

CREATE TABLE IF NOT EXISTS shadow_exchange_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('TRADE', 'FUNDING')),
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('VERIFIED_PENDING_LEDGER', 'POSTED', 'QUARANTINED')
    ),
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shadow_exchange_events_status_idx
ON shadow_exchange_events(status, occurred_at);

CREATE TABLE IF NOT EXISTS shadow_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
