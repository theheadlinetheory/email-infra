-- Inbox Groups: source of truth for all inbox group state
CREATE TABLE IF NOT EXISTS inbox_groups (
    id SERIAL PRIMARY KEY,
    group_letter TEXT NOT NULL,
    batch INT NOT NULL DEFAULT 1,
    smartlead_client_id INT NOT NULL,
    smartlead_client_name TEXT NOT NULL,
    assigned_client TEXT,
    role TEXT NOT NULL DEFAULT 'generic',
    status TEXT NOT NULL DEFAULT 'warming',
    account_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    account_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    domains JSONB NOT NULL DEFAULT '[]'::jsonb,
    campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    tag_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    daily_capacity INT NOT NULL DEFAULT 0,
    warmup_started DATE,
    warmup_ready DATE,
    drift_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(group_letter, batch)
);

CREATE INDEX IF NOT EXISTS idx_inbox_groups_status ON inbox_groups(status);
CREATE INDEX IF NOT EXISTS idx_inbox_groups_assigned_client ON inbox_groups(assigned_client);

-- Inbox Group History: append-only audit log
CREATE TABLE IF NOT EXISTS inbox_group_history (
    id SERIAL PRIMARY KEY,
    group_id INT NOT NULL REFERENCES inbox_groups(id),
    event TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    previous_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inbox_group_history_group_id ON inbox_group_history(group_id);
CREATE INDEX IF NOT EXISTS idx_inbox_group_history_created_at ON inbox_group_history(created_at);
