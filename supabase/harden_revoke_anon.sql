-- ═══════════════════════════════════════════════════════════
-- NOT APPLIED. Opt-in hardening for the email-infra project.
-- ═══════════════════════════════════════════════════════════
--
-- Found while auditing the 2026-10-30 Supabase grant change (2026-09-23).
--
-- Every table in this project has RLS DISABLED — that is explicit at the tail of
-- supabase_schema.sql ("we use the service_role key which bypasses RLS") and of
-- health_schema.sql. With RLS off, the GRANT is the only access control there
-- is. Supabase's old automatic default privileges granted anon and authenticated
-- SELECT/INSERT/UPDATE/DELETE on all 16 tables, so anyone holding this project's
-- publishable key can currently read and write the whole email infrastructure
-- database: inbox_groups, client_configs, pipelines, pending_deletions,
-- inbox_health_*, state.
--
-- Nothing in this repo uses that access. db.py authenticates with an
-- `sb_secret_…` (service_role) key; the browser bundle only calls this repo's
-- own /api/* endpoints and never talks to supabase.co.
--
-- ── Before running this, check the two things this repo cannot tell you ──
--   1. Nothing OUTSIDE this repo reads the project with the publishable key —
--      no Vercel edge function, no Retool/Sheets/Zapier connection, no one-off
--      script. Grep the org, not just this folder.
--   2. The two anon SELECT policies in migrations (anon_read_cache on `state`,
--      anon_read_sse_events on `sse_events`) really are vestigial. They were
--      written for an anon reader that no longer exists; revoking makes them
--      inert. If something does still poll sse_events with a publishable key,
--      keep a narrow `grant select on public.sse_events to anon` and re-enable
--      RLS on that table so the policy does the filtering.
--
-- Then apply in the SQL editor. There is no partial state: it is one
-- transaction.

begin;

do $revoke$
declare
  r record;
begin
  for r in
    select c.relname
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
  loop
    execute format('revoke all on public.%I from anon, authenticated', r.relname);
  end loop;

  for r in
    select c.relname
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'S'
  loop
    execute format('revoke all on sequence public.%I from anon, authenticated', r.relname);
  end loop;
end;
$revoke$;

-- Readback: expect zero rows. Any row is a table anon or authenticated can
-- still reach.
select table_name, grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and grantee in ('anon', 'authenticated')
order by table_name, grantee, privilege_type;

commit;
