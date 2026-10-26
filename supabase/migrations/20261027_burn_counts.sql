-- Burn tracking on counts, not rates (2026-10-27).
--
-- The count columns were declared in health_schema.sql on 2026-08-21 and never
-- migrated, so every stored day carried rates + sent only. That is why scoring
-- had to make a fresh windowed call to Smartlead on every run: nothing stored
-- could be added up. When that one call failed (a blank Smartlead session, an
-- expired fallback token) scoring did not run at all and the burned list froze.
--
-- With counts stored, a burn verdict is a SUM over the inbox's own recent rows:
-- "replies in its latest 200 sends". Smartlead being down then only means the
-- newest day is missing, never that the dashboard stops scoring.

alter table inbox_health_daily
  add column if not exists bounced           integer,
  add column if not exists replied           integer,
  add column if not exists opened            integer,
  add column if not exists positive_replied  integer,
  add column if not exists unique_lead_count integer;

-- The window a verdict was reached over, so the dashboard can say exactly
-- "0 replies in the latest 214 sends, since 2026-10-07".
alter table inbox_health_status
  add column if not exists win_sends     integer,
  add column if not exists win_replies   integer,
  add column if not exists win_bounces   integer,
  add column if not exists win_positives integer,
  add column if not exists win_from      date;

grant select, insert, update on inbox_health_daily  to service_role;
grant select, insert, update on inbox_health_status to service_role;
