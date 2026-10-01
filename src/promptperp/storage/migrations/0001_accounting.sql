PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investors (
    investor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verified_email_addresses (
    email TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_preferences (
    investor_id TEXT PRIMARY KEY REFERENCES investors(investor_id),
    frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    timezone TEXT NOT NULL,
    local_send_time TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS strategy_pools (
    pool_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL UNIQUE,
    strategy_version TEXT NOT NULL,
    base_asset TEXT NOT NULL CHECK (base_asset = 'USDT'),
    initial_unit_nav TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    investor_id TEXT PRIMARY KEY REFERENCES investors(investor_id),
    pool_id TEXT NOT NULL REFERENCES strategy_pools(pool_id),
    units TEXT NOT NULL,
    total_contributions TEXT NOT NULL,
    total_withdrawals TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_lots (
    lot_id TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    pool_id TEXT NOT NULL REFERENCES strategy_pools(pool_id),
    event_id TEXT NOT NULL UNIQUE,
    units TEXT NOT NULL,
    amount TEXT NOT NULL,
    lot_type TEXT NOT NULL CHECK (lot_type IN ('ISSUE', 'REDEEM')),
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL UNIQUE,
    source_event_id TEXT NOT NULL UNIQUE REFERENCES source_events(event_id),
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    external_reference TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    posting_index INTEGER NOT NULL,
    account TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    amount TEXT NOT NULL,
    asset TEXT NOT NULL CHECK (asset = 'USDT'),
    UNIQUE (transaction_id, posting_index)
);

CREATE INDEX IF NOT EXISTS ledger_entries_account_idx
ON ledger_entries(account);

CREATE TABLE IF NOT EXISTS reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    exchange_equity TEXT NOT NULL,
    internal_equity TEXT NOT NULL,
    difference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASSED', 'BLOCKED')),
    reasons_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS valuation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES strategy_pools(pool_id),
    occurred_at TEXT NOT NULL,
    pool_equity TEXT NOT NULL,
    outstanding_units TEXT NOT NULL,
    unit_nav TEXT NOT NULL,
    reconciliation_id TEXT NOT NULL REFERENCES reconciliations(reconciliation_id),
    UNIQUE (pool_id, reconciliation_id)
);

CREATE TABLE IF NOT EXISTS statements (
    statement_id TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES valuation_snapshots(snapshot_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (investor_id, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS email_outbox_messages (
    message_id TEXT PRIMARY KEY,
    statement_id TEXT NOT NULL REFERENCES statements(statement_id),
    recipient_email TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (statement_id, recipient_email)
);

CREATE TABLE IF NOT EXISTS operator_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
