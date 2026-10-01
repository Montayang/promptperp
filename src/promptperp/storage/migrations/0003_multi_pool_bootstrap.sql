ALTER TABLE subscriptions RENAME TO subscriptions_v1;

CREATE TABLE subscriptions (
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    pool_id TEXT NOT NULL REFERENCES strategy_pools(pool_id),
    units TEXT NOT NULL,
    total_contributions TEXT NOT NULL,
    total_withdrawals TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (investor_id, pool_id)
);

INSERT INTO subscriptions(
    investor_id, pool_id, units, total_contributions,
    total_withdrawals, active, updated_at
)
SELECT investor_id, pool_id, units, total_contributions,
       total_withdrawals, active, updated_at
FROM subscriptions_v1;

DROP TABLE subscriptions_v1;

ALTER TABLE report_preferences
ADD COLUMN monthly_send_day INTEGER NOT NULL DEFAULT 1
CHECK (monthly_send_day BETWEEN 1 AND 28);

CREATE TABLE imported_investor_snapshots (
    record_id TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    pool_id TEXT NOT NULL REFERENCES strategy_pools(pool_id),
    occurred_at TEXT NOT NULL,
    units TEXT NOT NULL,
    equity TEXT NOT NULL,
    unit_nav TEXT NOT NULL,
    source_note TEXT NOT NULL,
    UNIQUE (investor_id, pool_id, occurred_at)
);

CREATE INDEX imported_investor_snapshots_lookup_idx
ON imported_investor_snapshots(investor_id, pool_id, occurred_at);
