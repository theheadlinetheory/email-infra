# Supabase Data API grants

## The rule

Supabase used to keep an `ALTER DEFAULT PRIVILEGES` rule on the `public` schema that
granted every newly created table to `anon`, `authenticated` and `service_role`. A bare
`CREATE TABLE` was reachable through PostgREST immediately.

**That stopped on 2026-10-30.** A table created without an explicit `GRANT` still
exists and still accepts SQL from the editor, but every Data API call against it
returns `42501 permission denied for table <name>`.

After every `CREATE TABLE` in this project — in `supabase/migrations/`, in
`supabase_schema.sql`, in `health_schema.sql`, or typed into the SQL editor:

```sql
select public.grant_data_api('your_new_table');
```

The helper is installed by `supabase/migrations/20260923_data_api_grants.sql`. It is
idempotent, so running it twice costs nothing.

This matters more here than the migration folder suggests: **13 of the 16 live tables
were never created by a migration.** They came from `supabase_schema.sql` and
`health_schema.sql` pasted into the SQL editor. The guardrail has to be a habit in the
editor, not a checklist item in a file nobody opens.

`scripts/check_migration_grants.py` fails if a migration dated 2026-09-23 or later
creates a `public` table without granting it:

```
python3 scripts/check_migration_grants.py
```

## Why this project grants `service_role` only

The other two projects (fulfillment dashboard, CRM) grant
`anon, authenticated, service_role`, because their browser bundles talk to PostgREST
directly. This one does not:

- `db.py` authenticates with `SUPABASE_KEY`, which is an `sb_secret_…` key — that is
  `service_role`.
- `public/index.html` and `public/health.html` never contact `supabase.co`. They call
  this repo's own Python API (`/api/*`) and let `dashboard.py` do the database work.
- `/api/supabase-config` would hand a key to a browser, but nothing calls it.

So nothing needs `anon`, and there is a specific reason not to grant it:

> **RLS is disabled on every table in this project.** That is deliberate and explicit —
> see the tail of `supabase_schema.sql` ("we use the service_role key which bypasses
> RLS") and of `health_schema.sql`. With RLS off, **the grant is the only access
> control there is.** Granting `anon` on a table here publishes it for unauthenticated
> read *and write* to anyone holding the publishable key. No policy stands in the way.

## Open item: the inherited `anon` grants

The 16 tables that already existed keep the grants Supabase applied automatically over
the past months, which means `anon` currently has full `SELECT/INSERT/UPDATE/DELETE` on
all of them — `inbox_groups`, `client_configs`, `pipelines`, `pending_deletions`,
`inbox_health_*`, `state`. Nothing in this repo uses that access, and with RLS disabled
nothing constrains it either.

`supabase/harden_revoke_anon.sql` closes it. It is **written but not applied**, because
two things have to be checked first and neither can be settled from inside this repo:

1. Nothing outside this repo reads the project with the publishable key — no Vercel
   function, no Sheets/Retool/Zapier connection, no one-off script.
2. The two `anon` SELECT policies in `supabase/migrations/` (`anon_read_cache` on
   `state`, `anon_read_sse_events` on `sse_events`) really are vestigial.

The file documents both checks and what to do if the second one turns out to be wrong.
