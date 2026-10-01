ALTER TABLE email_outbox_messages RENAME TO email_outbox_messages_v3;

CREATE TABLE email_outbox_messages (
    message_id TEXT PRIMARY KEY,
    statement_id TEXT NOT NULL REFERENCES statements(statement_id),
    recipient_email TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    delivery_id TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (statement_id, recipient_email)
);

INSERT INTO email_outbox_messages(
    message_id, statement_id, recipient_email, payload_text,
    status, attempt_count, created_at, updated_at
)
SELECT message_id, statement_id, recipient_email, payload_text,
       status, attempt_count, created_at, updated_at
FROM email_outbox_messages_v3;

DROP TABLE email_outbox_messages_v3;

CREATE INDEX email_outbox_status_idx
ON email_outbox_messages(status, created_at, message_id);
