-- ═══════════════════════════════════════════════════════════
-- Data API grants — Supabase's automatic public-schema grant ends 2026-10-30
-- ═══════════════════════════════════════════════════════════
--
-- Supabase used to keep an ALTER DEFAULT PRIVILEGES rule on the public schema
-- that granted every new table to anon/authenticated/service_role. From
-- 2026-10-30 that rule is gone. A table created without an explicit GRANT still
-- exists and still accepts SQL, but every Data API call against it returns
-- 42501 "permission denied for table <name>".
--
-- Nothing live breaks on 2026-10-30. Verified against this project on
-- 2026-09-23: all 16 exposed tables answer 200 to the sb_secret key today, and
-- existing grants are kept. What breaks is the NEXT table anyone creates — and
-- in this project that is almost always a hand-run CREATE TABLE, since 13 of the
-- 16 live tables came from supabase_schema.sql / health_schema.sql pasted into
-- the SQL editor rather than from a file in this folder.
--
-- ── Role set for THIS project: service_role ONLY ─────────────────────────
-- This is narrower than the other two projects, on purpose:
--
--   * Nothing in this project reaches PostgREST as anon. db.py authenticates
--     with SUPABASE_KEY, which is an `sb_secret_…` key (service_role). The
--     browser side (public/index.html, public/health.html) never touches
--     supabase.co — it calls this repo's own Python API (/api/*) and lets
--     dashboard.py do the database work. /api/supabase-config would hand the
--     key to a browser, but nothing calls it.
--
--   * RLS is DISABLED on every table here (see the tail of supabase_schema.sql
--     and health_schema.sql). With RLS off, the GRANT is the only gate there
--     is. Granting anon on a new table would therefore publish it for
--     unauthenticated read AND write to anyone holding the publishable key —
--     no policy stands in the way.
--
-- This migration does NOT revoke anything, so the live project is unchanged:
-- the anon grants Supabase applied automatically over the past months are still
-- in place on the 16 existing tables. Closing that is a separate, deliberate
-- step — see supabase/harden_revoke_anon.sql, which is written but NOT applied.

grant usage on schema public to service_role;

-- ── 1. The helper ────────────────────────────────────────────────────────
-- Run in the SQL editor immediately after a hand-written CREATE TABLE:
--     select public.grant_data_api('my_new_table');
-- Idempotent, so re-running it is free.
create or replace function public.grant_data_api(p_table text)
returns void
language plpgsql
as $fn$
declare
  v_kind "char";
begin
  select c.relkind into v_kind
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relname = p_table;

  if v_kind is null then
    raise exception 'grant_data_api: public.% does not exist', p_table;
  end if;

  if v_kind = 'm' then
    execute format('grant select on public.%I to service_role', p_table);
  else
    execute format(
      'grant select, insert, update, delete on public.%I to service_role', p_table);
  end if;
end;
$fn$;

comment on function public.grant_data_api(text) is
  'Applies this project''s standard Data API grant (service_role only — this project is server-side and runs with RLS disabled) to a public table or view. Run after every CREATE TABLE — Supabase stopped granting new public tables automatically on 2026-10-30. Idempotent.';

revoke execute on function public.grant_data_api(text) from public;
grant execute on function public.grant_data_api(text) to postgres;

-- ── 2. Backfill everything that exists today ─────────────────────────────
-- GRANT only adds, so this changes nothing on the live project. It is what
-- makes a rebuild from this folder (db reset, preview branch, new project) come
-- up working after 2026-10-30 instead of 42501-ing on every read — and it
-- rebuilds WITHOUT the inherited anon grants, which is the state this project
-- should have been in all along.
do $backfill$
declare
  r record;
begin
  for r in
    select c.relname, c.relkind
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
  loop
    -- An object we do not own (an extension's table, if an extension is ever
    -- installed into public) cannot be granted, and an unhandled error here
    -- would abort the whole migration. Skip it loudly instead.
    begin
      if r.relkind = 'm' then
        execute format('grant select on public.%I to service_role', r.relname);
      else
        execute format(
          'grant select, insert, update, delete on public.%I to service_role', r.relname);
      end if;
    exception when insufficient_privilege then
      raise notice 'data_api_grants: skipped public.% (not owned by %)', r.relname, current_user;
    end;
  end loop;

  -- inbox_groups and inbox_group_history use SERIAL ids; sse_events,
  -- pending_deletions and monitor_log use identity columns. SERIAL inserts need
  -- USAGE on the sequence even when the table grant is present.
  for r in
    select c.relname
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'S'
  loop
    begin
      execute format(
        'grant usage, select on sequence public.%I to service_role', r.relname);
    exception when insufficient_privilege then
      raise notice 'data_api_grants: skipped sequence public.% (not owned by %)', r.relname, current_user;
    end;
  end loop;
end;
$backfill$;
