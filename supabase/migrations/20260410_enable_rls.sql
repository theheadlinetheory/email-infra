-- Enable RLS on the state table
alter table state enable row level security;

-- Allow anon role to SELECT cache keys only
create policy "anon_read_cache"
  on state
  for select
  to anon
  using (key like 'cache:%');
