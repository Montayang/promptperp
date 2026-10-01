CREATE TABLE report_schedule_state (
    investor_id TEXT PRIMARY KEY REFERENCES investors(investor_id),
    next_due_at TEXT NOT NULL,
    last_due_at TEXT,
    last_statement_id TEXT REFERENCES statements(statement_id),
    updated_at TEXT NOT NULL
);

CREATE INDEX report_schedule_due_idx
ON report_schedule_state(next_due_at, investor_id);
