-- Health V1 schema — per-inbox deliverability tracking.
-- Run in the Supabase SQL editor (same project as supabase_schema.sql).
-- Safe to re-run: everything is IF NOT EXISTS.

-- Per-inbox, per-day metric snapshot (the time-series we never had before).
create table if not exists inbox_health_daily (
    id           bigint generated always as identity primary key,
    email        text not null,
    date         date not null,
    client       text,
    group_letter text,               -- 'A' / 'B' / null
    source       text,               -- provider, e.g. 'Zapmail'
    domain       text,
    reply_rate   numeric,            -- % that day
    bounce_rate  numeric,            -- %
    ooo_rate     numeric,            -- % out-of-office replies (nullable until wired)
    placement    numeric,            -- inbox placement % from seed test (nullable)
    sent         int not null default 0,
    smtp_ok      boolean,            -- SMTP connection healthy (from overview cache)
    warmup_reputation numeric,       -- SmartLead warmup rep (info-only, not scored)
    created_at   timestamptz not null default now(),
    unique (email, date)             -- one row per inbox per day (upsert key)
);

create index if not exists idx_ihd_email on inbox_health_daily (email);
create index if not exists idx_ihd_date  on inbox_health_daily (date);

-- Current computed status per inbox (what the dashboard reads — one row each).
create table if not exists inbox_health_status (
    email        text primary key,
    score        int,                -- 0-100, null when warming / insufficient
    status       text not null,      -- healthy|watch|at_risk|burned|warming|insufficient
    reasons      jsonb not null default '[]',
    subscores    jsonb not null default '{}',
    client       text,
    group_letter text,
    source       text,
    domain       text,
    reply_3d     numeric,
    bounce_3d    numeric,
    ooo_3d       numeric,
    placement    numeric,
    sent_3d      int not null default 0,
    smtp_ok      boolean,
    warmup_reputation numeric,        -- info-only
    campaigns    jsonb not null default '[]',
    updated_at   timestamptz not null default now()
);

create index if not exists idx_ihs_status on inbox_health_status (status);
create index if not exists idx_ihs_domain on inbox_health_status (domain);
create index if not exists idx_ihs_client on inbox_health_status (client);

-- Tunable weights / thresholds (so weights change without a deploy).
-- Seeded with the V1 defaults agreed on the 2026-07-10 call.
create table if not exists inbox_health_config (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

insert into inbox_health_config (key, value)
values ('default', '{
    "weights": {"reply": 0.35, "bounce": 0.30, "ooo": 0.15, "placement": 0.20},
    "bounce_burn": 3.0, "bounce_risk": 2.5,
    "reply_dead": 0.3, "reply_risk": 0.8,
    "min_sent_3d": 30, "warmup_days": 14
}'::jsonb)
on conflict (key) do nothing;

alter table inbox_health_daily  disable row level security;
alter table inbox_health_status disable row level security;
alter table inbox_health_config disable row level security;
