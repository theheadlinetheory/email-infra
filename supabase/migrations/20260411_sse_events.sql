-- SSE events table for long-running operation progress tracking
create table if not exists sse_events (
    id bigint generated always as identity primary key,
    job_id text not null,
    step int not null default 0,
    status text not null default 'running',
    message text not null default '',
    data jsonb default '{}',
    created_at timestamptz not null default now()
);

create index if not exists idx_sse_job on sse_events (job_id, step);

-- Enable RLS — anon can read
alter table sse_events enable row level security;
create policy "anon_read_sse_events" on sse_events for select to anon using (true);
