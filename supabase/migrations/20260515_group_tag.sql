-- Add group_tag column to inbox_groups as the primary identity field.
-- This replaces reliance on smartlead_client_name + group_letter for identity.
alter table inbox_groups add column if not exists group_tag text;

-- Backfill: use smartlead_client_name as the initial group_tag value.
-- The migration script (migrate_to_group_tags.py) will update these to the correct format.
update inbox_groups set group_tag = smartlead_client_name where group_tag is null;

-- Index for fast lookups by group_tag
create index if not exists idx_inbox_groups_group_tag on inbox_groups(group_tag);
