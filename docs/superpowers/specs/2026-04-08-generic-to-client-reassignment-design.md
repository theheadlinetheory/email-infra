# Generic-to-Client Reassignment Feature

## Goal
Allow assigning a warmed-up generic infrastructure group (e.g., Generic D) to an actual client through the dashboard, propagating the change across SmartLead, Zapmail, Google Sheet, and the pipeline record.

## Trigger
"Assign to Client" button on any generic pipeline in the dashboard Pipelines tab.

## Modal UI
- Client dropdown (populated from SmartLead client list + Google Sheet clients, deduplicated)
- "Add New Client" option at bottom — reveals text input for new client name
- Forwarding domain text input
- "Assign" button (disabled until both fields filled)

## Progress Overlay
Replaces modal content on submit. Shows step list with live status:

1. Creating SmartLead client (if new) — spinner → checkmark
2. Updating SmartLead tags — spinner → checkmark
3. Updating SmartLead client assignment — spinner → checkmark
4. Updating Zapmail domain tags — spinner → checkmark
5. Setting forwarding domain — spinner → checkmark
6. Updating Google Sheet — spinner → checkmark
7. Updating pipeline record — spinner → checkmark

- Failed step: red X with error message, subsequent steps gray (skipped)
- Success: "Done" button
- Failure: "Retry" button

## Backend Endpoint

`POST /api/pipeline/assign-client` — streams progress via Server-Sent Events (SSE).

Request body: `{pipeline_id, client_name, forwarding_domain, is_new_client}`

### Step Details

**Step 1: SmartLead client**
- If existing client: find by name match in `GET /client?api_key=...`
- If new client: create via `POST /client/save?api_key=...`
- Return client ID

**Step 2: SmartLead tags**
- Get all tags via GraphQL `sl_get_all_tags()`
- Find the generic tag (e.g., "Generic D") — this gets removed
- Create new client name tag if it doesn't exist
- For every account in the group: call `sl_tag_account(account_id, [client_tag_id, zapmail_tag_id, date_tag_id], client_id=new_client_id)`
- This replaces the generic tag with the client tag while keeping Zapmail + date tags

**Step 3: SmartLead client assignment**
- Already handled in Step 2 via the `clientId` parameter in `sl_tag_account`
- Verify all accounts show correct client assignment

**Step 4: Zapmail domain tags**
- Find existing generic domain tag via `zm_list_domain_tags(workspace_id)`
- Create new client domain tag via `zm_create_domain_tag(client_name)`
- Assign new tag to all domains via `zm_assign_domain_tag(domain_ids, [new_tag_id])`
- Note: Zapmail tags are additive; the generic tag stays (no remove API). The client tag is what matters going forward.

**Step 5: Zapmail forwarding**
- Set forwarding domain on all domains via `zm_set_forwarding(domain_ids, forwarding_domain)`

**Step 6: Google Sheet**
- For each domain in the pipeline, update Notes column (D) from generic name to client name
- Uses `sheets.write_range()` with batch updates
- Also set up client tab via `sheets.setup_client_tab(client_name, domains)`

**Step 7: Pipeline record**
- Update pipeline in Supabase:
  - `client_name` → new client name
  - Add `original_group: "Generic D"` field
  - `updated_at` → now
- Save via `db.save_pipeline()`

## Client List Endpoint

`GET /api/clients/list` — returns deduplicated list of client names from SmartLead + Google Sheet for the dropdown.

## Architecture
- All logic in `dashboard.py` (new route handler + SSE streaming)
- Frontend in `dashboard.html` (modal + progress UI)
- Reuses existing API wrapper functions from `setup.py` and `pipeline.py`
- No new files needed

## Edge Cases
- Client name already exists in SmartLead: use existing client, don't create duplicate
- Zapmail tag API has no "remove tag" endpoint: add client tag alongside generic tag
- Pipeline is currently running: disable "Assign" button for non-complete pipelines
- Network error mid-assignment: show error on failed step, allow retry (idempotent operations)
