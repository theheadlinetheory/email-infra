-- Supabase schema for email infrastructure pipeline state
-- Run this in the Supabase SQL Editor after creating your project

-- Pipeline state (replaces pipelines/*.json)
create table if not exists pipelines (
    id text primary key,
    data jsonb not null,
    status text not null default 'running',
    client_name text not null default '',
    pipeline_type text not null default '',
    updated_at timestamptz not null default now()
);

create index if not exists idx_pipelines_status on pipelines (status);
create index if not exists idx_pipelines_client on pipelines (client_name);

-- Pending SmartLead account deletions (replaces pending_deletions.json)
create table if not exists pending_deletions (
    id bigint generated always as identity primary key,
    domain text not null,
    smartlead_account_ids jsonb not null default '[]',
    mailbox_ids jsonb not null default '[]',
    renewal_date text not null default '',
    removal_date text not null default '',
    client_name text not null default '',
    pipeline_id text not null default '',
    scheduled_at timestamptz not null default now()
);

create index if not exists idx_pending_domain on pending_deletions (domain);

-- A/B rotation state per client
create table if not exists client_rotations (
    client_name text primary key,
    group_a_ids jsonb not null default '[]',
    group_b_ids jsonb not null default '[]',
    active_group text not null default 'A',
    last_swap_date text not null default '',
    created_at timestamptz not null default now()
);

-- Client configs (replaces clients/*.json)
create table if not exists client_configs (
    id text primary key,
    client_name text not null,
    data jsonb not null,
    updated_at timestamptz not null default now()
);

-- Monitor audit log
create table if not exists monitor_log (
    id bigint generated always as identity primary key,
    event_type text not null,
    details jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create index if not exists idx_monitor_event on monitor_log (event_type);
create index if not exists idx_monitor_created on monitor_log (created_at);

-- Generic key-value state (placement test timestamps, etc.)
create table if not exists state (
    key text primary key,
    data jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

-- Infrastructure setup pipelines (dashboard-driven)
create table if not exists setup_pipelines (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    type text not null default 'generic',
    config jsonb not null default '{}',
    status text not null default 'pending',
    current_step int not null default 0,
    steps jsonb not null default '[]',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_setup_pipelines_status on setup_pipelines (status);

-- Inbox Groups: source of truth for all inbox group state
create table if not exists inbox_groups (
    id serial primary key,
    group_letter text not null,
    batch int not null default 1,
    smartlead_client_id int not null,
    smartlead_client_name text not null,
    assigned_client text,
    role text not null default 'generic',
    group_tag text,
    status text not null default 'warming',
    account_ids jsonb not null default '[]',
    account_emails jsonb not null default '[]',
    domains jsonb not null default '[]',
    campaign_ids jsonb not null default '[]',
    tag_ids jsonb not null default '[]',
    daily_capacity int not null default 0,
    warmup_started date,
    warmup_ready date,
    drift_flags jsonb not null default '[]',
    updated_at timestamptz not null default now(),
    unique(group_letter, batch)
);

create index if not exists idx_inbox_groups_status on inbox_groups(status);
create index if not exists idx_inbox_groups_assigned_client on inbox_groups(assigned_client);
create index if not exists idx_inbox_groups_group_tag on inbox_groups(group_tag);

-- Inbox Group History: append-only audit log
create table if not exists inbox_group_history (
    id serial primary key,
    group_id int not null references inbox_groups(id),
    event text not null,
    details jsonb not null default '{}',
    previous_state jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create index if not exists idx_inbox_group_history_group_id on inbox_group_history(group_id);
create index if not exists idx_inbox_group_history_created_at on inbox_group_history(created_at);

-- Disable RLS on all tables (we use the service_role key which bypasses RLS,
-- but direct PostgREST calls may still be blocked by default RLS policies)
alter table pipelines disable row level security;
alter table pending_deletions disable row level security;
alter table client_configs disable row level security;
alter table monitor_log disable row level security;
alter table state disable row level security;
alter table client_rotations disable row level security;
alter table setup_pipelines disable row level security;
alter table inbox_groups disable row level security;
alter table inbox_group_history disable row level security;
