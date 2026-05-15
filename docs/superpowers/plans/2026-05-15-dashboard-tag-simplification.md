# Dashboard Tag Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the dashboard so that a single SmartLead group tag is the source of truth for account identity — replacing the current mess of client_id + client tags + group concepts.

**Architecture:** New `tag_utils.py` module provides tag parsing functions used by dashboard.py and migration scripts. The `inbox_groups` Supabase table gets a `group_tag` column that becomes the primary key for group identity. All dashboard views (overview, acquisition, sync) switch from `client_id`-based grouping to tag-based grouping. Assignment modal gains A/B selection. Swap workflow gains reallocation reminder.

**Tech Stack:** Python 3 (dashboard.py, db.py, setup.py), vanilla JS (js/*.js), Supabase PostgreSQL, SmartLead API (REST + GraphQL + internal)

**Spec:** `docs/superpowers/specs/2026-05-15-dashboard-tag-simplification-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `tag_utils.py` | Create | Parse group tags, extract client name, determine role (generic/client/acquisition), extract A/B |
| `supabase/migrations/20260515_group_tag.sql` | Create | Add `group_tag` column to `inbox_groups` |
| `migrate_to_group_tags.py` | Create | One-time migration: update all existing SmartLead + Zapmail tags to new format |
| `db.py` | Modify | Add `group_tag` field handling, campaign exclusivity check |
| `dashboard.py` | Modify | Switch overview/acquisition/sync/assign/swap to tag-based grouping |
| `js/state.js` | Modify | Update ASSIGN_STEPS to include A/B selection step |
| `js/operations.js` | Modify | Add A/B dropdown to assignment modal, handle reallocation banner |
| `js/overview.js` | Modify | Render A/B groups side-by-side on client cards, show reallocation banner |
| `dashboard.html` | Modify | Add A/B select to assignment modal, add reallocation banner markup |
| `setup.py` | Modify | Update `calculate_infra` to enforce 14 domain / 42 account standard |

---

### Task 1: Tag Parsing Utility

**Files:**
- Create: `tag_utils.py`

- [ ] **Step 1: Create `tag_utils.py` with parsing functions**

```python
"""Tag parsing utilities for the THT infrastructure dashboard.

Group tag format:
  - Generic:     "Generic F", "Generic G2"
  - Client:      "Kay's Landscaping A", "Pioneer Landscaping B"
  - Acquisition: "Acquisition A", "Acquisition H"
"""

import re

ZAPMAIL_TAG_ID = 262254


def parse_group_tag(tag_name: str) -> dict:
    """Parse a group tag into its components.

    Returns dict with keys: role, client_name, group_letter, raw.
    - role: "generic", "client", or "acquisition"
    - client_name: None for generic/acquisition, client name for client groups
    - group_letter: "A", "B", "F", "G2", "H", etc.
    - raw: the original tag string
    """
    tag = tag_name.strip()

    if tag.lower().startswith("generic"):
        letter = tag[len("generic"):].strip()
        return {"role": "generic", "client_name": None, "group_letter": letter, "raw": tag}

    if tag.lower().startswith("acquisition"):
        letter = tag[len("acquisition"):].strip()
        return {"role": "acquisition", "client_name": None, "group_letter": letter, "raw": tag}

    # Client group: everything before the last " A" or " B" (single uppercase letter)
    m = re.match(r'^(.+)\s+([A-Z](?:\d+)?)$', tag)
    if m:
        return {"role": "client", "client_name": m.group(1), "group_letter": m.group(2), "raw": tag}

    # Fallback: can't parse, treat as generic
    return {"role": "generic", "client_name": None, "group_letter": tag, "raw": tag}


def get_group_tag_from_account(account: dict) -> str | None:
    """Extract the group tag from a SmartLead account's tags array.

    The group tag is the one that is NOT "Zapmail" and NOT a date pattern (M/D/YY).
    """
    for t in account.get("tags", []):
        name = t.get("name", "")
        if name.lower() == "zapmail":
            continue
        if re.match(r'^\d{1,2}/\d{1,2}/\d{2}$', name):
            continue
        return name
    return None


def build_client_group_tag(client_name: str, ab: str) -> str:
    """Build a client group tag from client name and A/B designation."""
    return f"{client_name} {ab.upper()}"


def build_acquisition_tag(letter: str) -> str:
    """Build an acquisition group tag from a group letter."""
    return f"Acquisition {letter.upper()}"


def build_generic_tag(letter: str) -> str:
    """Build a generic group tag from a group letter."""
    return f"Generic {letter}"


def group_accounts_by_tag(accounts: list[dict]) -> dict[str, list[dict]]:
    """Group a list of SmartLead accounts by their group tag.

    Returns {group_tag_string: [account, ...]}.
    Accounts with no parseable group tag go under key "__untagged__".
    """
    groups = {}
    for acc in accounts:
        tag = get_group_tag_from_account(acc)
        key = tag or "__untagged__"
        groups.setdefault(key, []).append(acc)
    return groups
```

- [ ] **Step 2: Verify the module loads**

Run: `cd ~/email-infra && python3 -c "from tag_utils import parse_group_tag, get_group_tag_from_account; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Quick smoke test of parsing**

Run:
```bash
cd ~/email-infra && python3 -c "
from tag_utils import parse_group_tag
tests = [
    ('Generic F', 'generic', None, 'F'),
    ('Generic G2', 'generic', None, 'G2'),
    (\"Kay's Landscaping A\", 'client', \"Kay's Landscaping\", 'A'),
    ('Pioneer Landscaping B', 'client', 'Pioneer Landscaping', 'B'),
    ('Acquisition A', 'acquisition', None, 'A'),
    ('Acquisition H', 'acquisition', None, 'H'),
]
for tag, exp_role, exp_client, exp_letter in tests:
    r = parse_group_tag(tag)
    assert r['role'] == exp_role, f'{tag}: role {r[\"role\"]} != {exp_role}'
    assert r['client_name'] == exp_client, f'{tag}: client {r[\"client_name\"]} != {exp_client}'
    assert r['group_letter'] == exp_letter, f'{tag}: letter {r[\"group_letter\"]} != {exp_letter}'
print('All parsing tests passed')
"
```
Expected: `All parsing tests passed`

- [ ] **Step 4: Commit**

```bash
git add tag_utils.py
git commit -m "feat: add tag_utils.py for group tag parsing"
```

---

### Task 2: Database Schema Migration

**Files:**
- Create: `supabase/migrations/20260515_group_tag.sql`
- Modify: `db.py:402-466` (inbox group functions)

- [ ] **Step 1: Create the SQL migration**

Create `supabase/migrations/20260515_group_tag.sql`:

```sql
-- Add group_tag column to inbox_groups as the primary identity field.
-- This replaces reliance on smartlead_client_name + group_letter for identity.
alter table inbox_groups add column if not exists group_tag text;

-- Backfill: use smartlead_client_name as the initial group_tag value.
-- The migration script (migrate_to_group_tags.py) will update these to the correct format.
update inbox_groups set group_tag = smartlead_client_name where group_tag is null;

-- Index for fast lookups by group_tag
create index if not exists idx_inbox_groups_group_tag on inbox_groups(group_tag);
```

- [ ] **Step 2: Apply the migration**

Run: `cd ~/email-infra && cat supabase/migrations/20260515_group_tag.sql | python3 -c "import db; print('Migration needs to be applied via Supabase dashboard or CLI')"`

Apply via Supabase SQL editor: copy the contents of `supabase/migrations/20260515_group_tag.sql` and run it.

- [ ] **Step 3: Update `db.py` — add `group_tag` to JSON column list**

In `db.py`, the functions `get_all_inbox_groups`, `get_inbox_group`, `get_inbox_group_by_id`, `upsert_inbox_group`, and `update_inbox_group` all process JSON columns. `group_tag` is a text column, not JSON, so no changes needed for serialization. But add a helper function for the exclusivity check.

Add at line 467 (after `update_inbox_group`):

```python
def check_campaign_exclusivity(group_id: int, campaign_id: int) -> dict | None:
    """Check if a group is already in an active campaign.

    Returns None if clear, or a dict with the conflicting campaign info.
    """
    group = get_inbox_group_by_id(group_id)
    if not group:
        return None
    existing = group.get("campaign_ids") or []
    if isinstance(existing, str):
        existing = json.loads(existing)
    for cid in existing:
        if cid != campaign_id and cid:
            return {"group_id": group_id, "group_tag": group.get("group_tag", ""), "conflicting_campaign_id": cid}
    return None
```

- [ ] **Step 4: Add `get_inbox_group_by_tag` lookup function**

Add after the new `check_campaign_exclusivity` function in `db.py`:

```python
def get_inbox_group_by_tag(group_tag: str) -> dict | None:
    """Get an inbox group by its group_tag."""
    rows = _request("GET", "/inbox_groups", params={
        "select": "*",
        "group_tag": f"eq.{group_tag}",
    })
    if not rows:
        return None
    r = rows[0]
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if isinstance(r.get(col), str):
            r[col] = json.loads(r[col])
    return r
```

- [ ] **Step 5: Update `supabase_schema.sql` to include `group_tag` for new installs**

In `supabase_schema.sql`, add `group_tag text,` after the `role` column in the `inbox_groups` table definition (around line 91).

- [ ] **Step 6: Commit**

```bash
git add supabase/migrations/20260515_group_tag.sql db.py supabase_schema.sql
git commit -m "feat: add group_tag column to inbox_groups, add exclusivity check"
```

---

### Task 3: Update `assign_client_sse` — New Group Tag Format

**Files:**
- Modify: `dashboard.py:3360-3511` (assign_client_sse, Step 2 tag logic)

- [ ] **Step 1: Add `tag_utils` import to dashboard.py**

At the top of `dashboard.py` (around line 49, after the other imports), add:

```python
from tag_utils import (
    parse_group_tag, get_group_tag_from_account, build_client_group_tag,
    build_acquisition_tag, build_generic_tag, group_accounts_by_tag,
    ZAPMAIL_TAG_ID,
)
```

- [ ] **Step 2: Update `assign_client_sse` to accept `ab_group` parameter**

Change the function signature at line 3360 from:

```python
def assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client):
```

to:

```python
def assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client, ab_group="A"):
```

- [ ] **Step 3: Update Step 2 (tag assignment) to use group tag**

Replace the tag building block at lines 3495-3502 (the `# Build the required 3-tag list` section):

Old:
```python
        # Build the required 3-tag list
        required_tags = []
        if zapmail_tag_id:
            required_tags.append(zapmail_tag_id)
        if client_tag_id:
            required_tags.append(client_tag_id)
        if date_tag_id:
            required_tags.append(date_tag_id)
```

New:
```python
        # Build the required 3-tag list: [Zapmail, GroupTag, WarmupDate]
        group_tag_name = build_client_group_tag(client_name, ab_group)
        group_tag_id = sl_find_or_create_tag(group_tag_name, existing_tags=all_tags)

        required_tags = []
        if zapmail_tag_id:
            required_tags.append(zapmail_tag_id)
        if group_tag_id:
            required_tags.append(group_tag_id)
        if date_tag_id:
            required_tags.append(date_tag_id)
```

Also remove the old `client_tag_id` line (line 3430):
```python
        client_tag_id = sl_find_or_create_tag(client_name, existing_tags=all_tags)
```

Replace with a comment:
```python
        # Group tag replaces the old client-name-only tag
```

- [ ] **Step 4: Update the Supabase inbox_group record after assignment**

After the pipeline record update (around line 3600), add:

```python
        # Update inbox_group record with new group_tag
        ig = store.get_inbox_group_by_tag(original_name)
        if ig:
            store.update_inbox_group(ig["id"],
                group_tag=group_tag_name,
                assigned_client=client_name,
                role="client",
                status="active",
            )
            store.log_group_event(ig["id"], "assigned_to_client", {
                "client_name": client_name,
                "ab_group": ab_group,
                "old_tag": original_name,
                "new_tag": group_tag_name,
            })
```

- [ ] **Step 5: Update the API endpoint to pass `ab_group`**

Find where `/api/pipeline/assign-client` is handled (around line 4105-4115). Update to extract `ab_group` from the request body:

```python
                    ab_group = body.get("ab_group", "A")
```

And pass it to the SSE call:

```python
                    for chunk in assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client, ab_group):
```

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat: assign_client_sse uses group tag format (Client Name A/B)"
```

---

### Task 4: Update `transition_client_sse` — Group Tag Format

**Files:**
- Modify: `dashboard.py:3172-3332` (transition_client_sse)

- [ ] **Step 1: Update tag replacement logic**

In `transition_client_sse`, the current logic (around lines 3231-3253) does a surgical tag swap — removes old client tag, adds new client tag. Update it to work with group tags.

Find the section that builds new tags per account (around line 3244-3247). Replace the tag swap logic:

Old pattern:
```python
                    current_tags = [t["id"] for t in acc.get("tags", [])]
                    new_tags = [t for t in current_tags if t != old_tag_id]
                    if new_tag_id and new_tag_id not in new_tags:
                        new_tags.append(new_tag_id)
```

New pattern:
```python
                    # Get current group tag to determine A/B designation
                    old_group_tag = get_group_tag_from_account(acc)
                    parsed = parse_group_tag(old_group_tag or "")
                    ab = parsed["group_letter"] if parsed["role"] == "client" else "A"

                    new_group_tag_name = build_client_group_tag(new_client_name, ab)
                    new_group_tag_id = sl_find_or_create_tag(new_group_tag_name, existing_tags=all_tags)

                    # Rebuild: [Zapmail, new group tag, warmup date]
                    new_tags = []
                    for t in acc.get("tags", []):
                        if t.get("name", "").lower() == "zapmail":
                            new_tags.append(t["id"])
                        elif re.match(r'^\d{1,2}/\d{1,2}/\d{2}$', t.get("name", "")):
                            new_tags.append(t["id"])
                    if new_group_tag_id not in new_tags:
                        new_tags.append(new_group_tag_id)
```

- [ ] **Step 2: Add `re` import if not already present and `tag_utils` imports at usage point**

The `re` module should already be imported at the top of `dashboard.py`. Verify. The `tag_utils` import was added in Task 3.

- [ ] **Step 3: Update inbox_group record after transition**

After the transition completes, update the Supabase inbox_group:

```python
        # Update inbox_group record
        ig = store.get_inbox_group_by_tag(old_group_tag)
        if ig:
            store.update_inbox_group(ig["id"],
                group_tag=new_group_tag_name,
                assigned_client=new_client_name,
            )
```

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: transition_client_sse preserves A/B in group tag"
```

---

### Task 5: Update `_compute_overview` — Tag-Based Grouping

**Files:**
- Modify: `dashboard.py:542-868` (_compute_overview)

- [ ] **Step 1: Replace client_id grouping with tag-based grouping**

The current code at lines 566-578 splits accounts by `client_id`. Replace this entire classification block.

Replace lines 566-578:

Old:
```python
    # Classify clients as acquisition vs client/generic
    def _is_acquisition_group(name):
        nl = name.lower()
        return ("group" in nl and ("/" in name or "day" in nl)) or nl == "acquisition inboxes"

    def _is_generic_group(name):
        return name.lower().startswith("generic")

    acq_client_ids = {cl["id"] for cl in clients if _is_acquisition_group(cl.get("name", ""))}

    # Split accounts into client+generic vs acquisition
    client_accounts = [a for a in all_accounts if a.get("client_id") not in acq_client_ids]
    acq_accounts = [a for a in all_accounts if a.get("client_id") in acq_client_ids]
```

New:
```python
    # Classify accounts by parsing their group tag
    client_accounts = []
    acq_accounts = []
    generic_accounts = []
    untagged_accounts = []
    for a in all_accounts:
        group_tag = get_group_tag_from_account(a)
        a["_group_tag"] = group_tag  # cache for later use
        if not group_tag:
            untagged_accounts.append(a)
        else:
            parsed = parse_group_tag(group_tag)
            a["_parsed_tag"] = parsed
            if parsed["role"] == "acquisition":
                acq_accounts.append(a)
            elif parsed["role"] == "generic":
                generic_accounts.append(a)
            else:
                client_accounts.append(a)
```

- [ ] **Step 2: Update the per-client loop to group by tag instead of client_id**

Replace the per-client loop at lines 601-608:

Old:
```python
    client_summaries = []
    for cl in clients:
        cl_name = cl.get("name", "")
        if _is_acquisition_group(cl_name) or _is_generic_group(cl_name):
            continue
        if not is_crm_client(cl_name, crm_names):
            continue
        cl_accounts = [a for a in all_accounts if a.get("client_id") == cl["id"]]
```

New:
```python
    # Group client accounts by client name (derived from tag)
    client_groups = {}
    for a in client_accounts:
        parsed = a.get("_parsed_tag", {})
        cl_name = parsed.get("client_name", "")
        if cl_name:
            client_groups.setdefault(cl_name, []).append(a)

    client_summaries = []
    for cl_name, cl_accounts in client_groups.items():
        if not is_crm_client(cl_name, crm_names):
            continue
```

The rest of the per-client loop (warmup dates, health metrics, batch bucketing) stays the same — it already operates on `cl_accounts`.

- [ ] **Step 3: Add A/B breakdown to client summary**

After the existing per-client stats calculation (around line 690 where the summary dict is built), add A/B group info:

```python
        # Split into A and B groups by tag
        a_group = [a for a in cl_accounts if a.get("_parsed_tag", {}).get("group_letter") == "A"]
        b_group = [a for a in cl_accounts if a.get("_parsed_tag", {}).get("group_letter") == "B"]
        other_group = [a for a in cl_accounts if a.get("_parsed_tag", {}).get("group_letter") not in ("A", "B")]
```

Add these to the summary dict that gets appended to `client_summaries`:

```python
            "group_a_count": len(a_group),
            "group_b_count": len(b_group),
            "group_a_tag": f"{cl_name} A" if a_group else None,
            "group_b_tag": f"{cl_name} B" if b_group else None,
```

- [ ] **Step 4: Update unassigned count to use untagged_accounts**

Replace line 588:

Old:
```python
    unassigned = sum(1 for a in all_accounts if not a.get("client_id"))
```

New:
```python
    unassigned = len(untagged_accounts)
```

- [ ] **Step 5: Commit**

```bash
git add dashboard.py
git commit -m "feat: _compute_overview groups accounts by tag, not client_id"
```

---

### Task 6: Update `api_acquisition` — Tag-Based Grouping

**Files:**
- Modify: `dashboard.py:1558-1670` (api_acquisition)

- [ ] **Step 1: Replace client_id grouping with tag grouping**

The current `api_acquisition` at line 1558 fetches all accounts and filters by client_id. Replace the grouping logic to use tags.

Find the section that identifies acquisition clients (around lines 1567-1571) and builds groups. Replace the grouping with:

```python
    # Group accounts by acquisition tag
    tag_groups = group_accounts_by_tag(all_accounts)
    acq_groups_raw = {}
    for tag_name, accs in tag_groups.items():
        parsed = parse_group_tag(tag_name)
        if parsed["role"] == "acquisition":
            acq_groups_raw[tag_name] = {"letter": parsed["group_letter"], "accounts": accs}
```

Then build the group summaries from `acq_groups_raw` instead of from client_id-based filtering.

- [ ] **Step 2: Commit**

```bash
git add dashboard.py
git commit -m "feat: api_acquisition groups by Acquisition tag, not client_id"
```

---

### Task 7: Update Swap Workflow — Supabase-First + Reallocation Reminder

**Files:**
- Modify: `dashboard.py:3612-3699` (swap_client_group)

- [ ] **Step 1: Update `swap_client_group` to use tags for A/B identity**

Replace the current function. The key change: instead of reading `group_a_ids` / `group_b_ids` from `client_rotations`, read them from `inbox_groups` by finding groups with tags `{client_name} A` and `{client_name} B`.

Replace lines 3612-3699:

```python
def swap_client_group(client_name):
    """Swap a client's active group in all their campaigns.

    Uses group tags to find A/B groups. Updates Supabase first, then SmartLead.
    Returns dict with results including reallocation reminder.
    """
    tag_a = build_client_group_tag(client_name, "A")
    tag_b = build_client_group_tag(client_name, "B")

    group_a = store.get_inbox_group_by_tag(tag_a)
    group_b = store.get_inbox_group_by_tag(tag_b)

    if not group_a and not group_b:
        return {"error": f"No A/B groups found for '{client_name}'"}

    # Determine which is active by checking status
    if group_a and group_a.get("status") == "active":
        outgoing, incoming = group_a, group_b
        new_active = "B"
    elif group_b and group_b.get("status") == "active":
        outgoing, incoming = group_b, group_a
        new_active = "A"
    else:
        # Fallback to rotation table
        rotation = store.get_rotation(client_name)
        if rotation and rotation["active_group"] == "A":
            outgoing, incoming = group_a, group_b
            new_active = "B"
        else:
            outgoing, incoming = group_b, group_a
            new_active = "A"

    if not incoming:
        return {"error": f"Group {new_active} not found for '{client_name}' — cannot swap"}

    outgoing_ids = set(outgoing.get("account_ids") or [])
    incoming_ids = incoming.get("account_ids") or []

    if not incoming_ids:
        return {"error": f"Group {new_active} has no accounts — cannot swap"}

    # Update Supabase FIRST (intent)
    today = datetime.now().strftime("%Y-%m-%d")
    store.update_inbox_group(outgoing["id"], status="resting", campaign_ids=[])
    store.update_inbox_group(incoming["id"], status="active")

    # Find campaigns containing outgoing accounts
    _sl_rate.wait()
    r = requests.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
    all_campaigns = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    active_campaigns = [c for c in all_campaigns if c.get("status") in ("ACTIVE", "PAUSED")]

    campaigns_updated = []
    for camp in active_campaigns:
        _sl_rate.wait()
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        if cr.status_code != 200:
            continue
        camp_accounts = cr.json() if isinstance(cr.json(), list) else []
        camp_account_ids = {ca["id"] for ca in camp_accounts}
        if camp_account_ids & outgoing_ids:
            campaigns_updated.append({"id": camp["id"], "name": camp.get("name", "")})
        time.sleep(0.2)

    if not campaigns_updated:
        return {"error": f"No campaigns found with outgoing accounts for {client_name}"}

    # Add incoming, remove outgoing
    for camp in campaigns_updated:
        _sl_rate.wait()
        requests.post(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": incoming_ids},
            timeout=30,
        )
        time.sleep(0.3)

    for camp in campaigns_updated:
        _sl_rate.wait()
        requests.delete(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": list(outgoing_ids)},
            timeout=30,
        )
        time.sleep(0.3)

    # Update Supabase with campaign IDs for incoming group
    campaign_ids = [c["id"] for c in campaigns_updated]
    store.update_inbox_group(incoming["id"], campaign_ids=campaign_ids)

    # Update rotation record
    store.update_rotation_swap(client_name, new_active, today)

    # Log event
    if outgoing.get("id"):
        store.log_group_event(outgoing["id"], "swapped_out", {"new_active": new_active})
    if incoming.get("id"):
        store.log_group_event(incoming["id"], "swapped_in", {"campaigns": campaign_ids})

    return {
        "ok": True,
        "client_name": client_name,
        "previous_group": "A" if new_active == "B" else "B",
        "new_group": new_active,
        "campaigns_updated": len(campaigns_updated),
        "campaign_names": [c["name"] for c in campaigns_updated],
        "accounts_added": len(incoming_ids),
        "accounts_removed": len(outgoing_ids),
        "reallocation_required": True,
    }
```

- [ ] **Step 2: Store dismissed reallocation banners in Supabase state**

In `dashboard.py`, find the swap API handler (around line 4171). After calling `swap_client_group`, persist the reallocation reminder:

```python
                    if result.get("ok"):
                        store.set_state(f"realloc_reminder_{result['client_name']}", {
                            "client_name": result["client_name"],
                            "new_group": result["new_group"],
                            "swap_date": datetime.now().isoformat(),
                            "dismissed": False,
                        })
```

Add a dismiss endpoint. Find the route handler section and add:

```python
                elif path == "/api/rotation/dismiss-reminder":
                    client_name = body.get("client_name", "")
                    store.set_state(f"realloc_reminder_{client_name}", {
                        "dismissed": True,
                    })
                    self._json_response({"ok": True})
```

- [ ] **Step 3: Include reallocation reminders in overview response**

In `_compute_overview`, before the return statement, fetch active reminders:

```python
    # Fetch undismissed reallocation reminders
    realloc_reminders = {}
    for cl_name in client_groups:
        reminder = store.get_state(f"realloc_reminder_{cl_name}")
        if reminder and not reminder.get("dismissed"):
            realloc_reminders[cl_name] = reminder
```

Add `"realloc_reminders": realloc_reminders` to the return dict.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: swap uses tags for A/B identity, adds reallocation reminder"
```

---

### Task 8: Update Assignment Modal — A/B Selection

**Files:**
- Modify: `dashboard.html:256-286` (assignment modal)
- Modify: `js/operations.js:124-187` (openAssignModal, startAssignment)
- Modify: `js/state.js:27-35` (ASSIGN_STEPS)

- [ ] **Step 1: Add A/B dropdown to assignment modal HTML**

In `dashboard.html`, after the client select div (line 267) and before the new client row (line 268), add:

```html
        <div style="margin-bottom:16px;">
            <label>Group Designation</label>
            <select id="ac-ab-group" onchange="checkAssignReady()">
                <option value="A">A Group (primary)</option>
                <option value="B">B Group (reserve)</option>
            </select>
        </div>
```

- [ ] **Step 2: Update `startAssignment` in `js/operations.js` to send `ab_group`**

In `js/operations.js` around line 158, in the `startAssignment` function, add after the `fwd` variable:

```javascript
    var abGroup = document.getElementById('ac-ab-group').value;
```

Update the `runSSEOperation` call to include `ab_group`:

```javascript
    runSSEOperation(
        '/api/pipeline/assign-client',
        {pipeline_id: pipelineId, client_name: clientName, forwarding_domain: fwd, is_new_client: isNew, ab_group: abGroup},
```

- [ ] **Step 3: Update ASSIGN_STEPS in `js/state.js`**

Replace the ASSIGN_STEPS constant (lines 27-35):

```javascript
var ASSIGN_STEPS = [
    {id: 1, label: 'Creating SmartLead client'},
    {id: 2, label: 'Setting group tags'},
    {id: 3, label: 'Verifying client assignment'},
    {id: 4, label: 'Updating Zapmail domain tags'},
    {id: 5, label: 'Setting forwarding domain'},
    {id: 6, label: 'Updating Google Sheet'},
    {id: 7, label: 'Updating pipeline record'},
];
```

(Only change: step 2 label from "Updating SmartLead tags" to "Setting group tags")

- [ ] **Step 4: Commit**

```bash
git add dashboard.html js/operations.js js/state.js
git commit -m "feat: assignment modal includes A/B group selection"
```

---

### Task 9: Update Client Card Rendering — A/B Side-by-Side + Reallocation Banner

**Files:**
- Modify: `js/overview.js:151-239` (renderCardHTML)

- [ ] **Step 1: Add A/B breakdown to client card**

In `renderCardHTML` in `js/overview.js`, after the existing stats section (around line 220), add A/B group info:

```javascript
    if (item.group_a_count || item.group_b_count) {
        html += '<div style="display:flex;gap:12px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">';
        if (item.group_a_tag) {
            var aActive = item.active_group === 'A';
            html += '<div style="flex:1;padding:6px 8px;border-radius:6px;background:' + (aActive ? 'rgba(64,224,208,0.1)' : 'rgba(255,255,255,0.03)') + ';">';
            html += '<div style="font-size:11px;font-weight:600;color:' + (aActive ? 'var(--accent)' : 'var(--text-muted)') + ';">Group A' + (aActive ? ' ● Active' : ' ○ Resting') + '</div>';
            html += '<div style="font-size:11px;color:var(--text-muted);">' + item.group_a_count + ' accounts</div>';
            html += '</div>';
        }
        if (item.group_b_tag) {
            var bActive = item.active_group === 'B';
            html += '<div style="flex:1;padding:6px 8px;border-radius:6px;background:' + (bActive ? 'rgba(64,224,208,0.1)' : 'rgba(255,255,255,0.03)') + ';">';
            html += '<div style="font-size:11px;font-weight:600;color:' + (bActive ? 'var(--accent)' : 'var(--text-muted)') + ';">Group B' + (bActive ? ' ● Active' : ' ○ Resting') + '</div>';
            html += '<div style="font-size:11px;color:var(--text-muted);">' + item.group_b_count + ' accounts</div>';
            html += '</div>';
        }
        html += '</div>';
    }
```

- [ ] **Step 2: Add reallocation banner to client card**

Before the A/B breakdown, check for active reallocation reminders:

```javascript
    var reminder = (overviewData.realloc_reminders || {})[item.name];
    if (reminder && !reminder.dismissed) {
        html += '<div style="background:#7f1d1d;border:1px solid #ef4444;border-radius:8px;padding:10px 14px;margin-top:8px;display:flex;justify-content:space-between;align-items:center;">';
        html += '<span style="color:#fca5a5;font-size:12px;font-weight:600;">⚠ Inboxes swapped. Reallocate inboxes in SmartLead now or sends will be 0/day.</span>';
        html += '<button onclick="dismissRealloc(\'' + item.name.replace(/'/g, "\\'") + '\')" style="background:none;border:1px solid #ef4444;color:#ef4444;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;">Dismiss</button>';
        html += '</div>';
    }
```

- [ ] **Step 3: Add `dismissRealloc` function**

Add to `js/operations.js`:

```javascript
function dismissRealloc(clientName) {
    fetch('/api/rotation/dismiss-reminder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({client_name: clientName}),
    }).then(function() { loadOverview(); });
}
```

- [ ] **Step 4: Commit**

```bash
git add js/overview.js js/operations.js
git commit -m "feat: client cards show A/B groups side-by-side + reallocation banner"
```

---

### Task 10: Update Sync Check — Group Tag Comparison

**Files:**
- Modify: `dashboard.py:2073-2126` (api_zapmail_sync)

- [ ] **Step 1: Replace client_id-based sync with group tag-based sync**

Replace the entire `api_zapmail_sync` function:

```python
def api_zapmail_sync():
    """Check ZapMail domain tags vs SmartLead group tags for mismatches."""
    zm_domains = zm_list_domains()
    sl_accounts = get_all_accounts()

    # Build domain -> ZapMail tag
    zm_tag_by_domain = {}
    for d in zm_domains:
        tags = [t.get("name", "") for t in d.get("tags", [])]
        if tags:
            zm_tag_by_domain[d["domain"]] = tags[0]

    # Build domain -> SmartLead group tag (from account tags, not client_id)
    sl_tag_by_domain = {}
    for a in sl_accounts:
        email = a.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        group_tag = get_group_tag_from_account(a)
        if domain and group_tag:
            sl_tag_by_domain[domain] = group_tag

    # Find mismatches
    mismatches = []
    all_domains = set(zm_tag_by_domain.keys()) | set(sl_tag_by_domain.keys())
    for domain in sorted(all_domains):
        zm_tag = zm_tag_by_domain.get(domain)
        sl_tag = sl_tag_by_domain.get(domain)
        if zm_tag and sl_tag:
            zm_lower = zm_tag.lower().strip()
            sl_lower = sl_tag.lower().strip()
            if zm_lower != sl_lower and zm_lower not in sl_lower and sl_lower not in zm_lower:
                mismatches.append({
                    "domain": domain,
                    "zapmail_tag": zm_tag,
                    "smartlead_tag": sl_tag,
                })

    zm_only = [d for d in zm_tag_by_domain if d not in sl_tag_by_domain]
    sl_only = [d for d in sl_tag_by_domain if d not in zm_tag_by_domain]

    return {
        "mismatches": mismatches,
        "zapmail_only_count": len(zm_only),
        "smartlead_only_count": len(sl_only),
        "zapmail_only": sorted(zm_only)[:20],
        "smartlead_only": sorted(sl_only)[:20],
        "total_checked": len(all_domains),
    }
```

- [ ] **Step 2: Update `js/secondary-tabs.js` to show "SmartLead Tag" instead of "SmartLead Client"**

Find the mismatch rendering (around line 419) and update the label from `smartlead_client` to `smartlead_tag`:

Old:
```javascript
`ZapMail: <span class="mismatch">${m.zapmail_tag}</span> vs SmartLead: <span class="mismatch">${m.smartlead_client}</span>`
```

New:
```javascript
`ZapMail: <span class="mismatch">${m.zapmail_tag}</span> vs SmartLead: <span class="mismatch">${m.smartlead_tag}</span>`
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.py js/secondary-tabs.js
git commit -m "feat: sync check compares group tags instead of client_id"
```

---

### Task 11: Campaign Exclusivity Enforcement

**Files:**
- Modify: `dashboard.py` (acquisition assignment handler around line 1890)

- [ ] **Step 1: Add exclusivity check to acquisition group assignment**

In the acquisition assignment handler (around line 1894), replace the current conflict check:

Old:
```python
        # Conflict check: ensure no account is already in another ACTIVE acquisition campaign
        campaign_details = get_global_campaign_details()
        conflicts = set()
        for a in group_accounts:
            email = a.get("from_email", "")
            for camp in campaign_details.get(email, []):
                if (camp["status"] == "ACTIVE" and camp["id"] != campaign_id
                        and "acquisition" in camp.get("name", "").lower()):
                    conflicts.add(camp["name"])
```

New:
```python
        # Exclusivity check: use Supabase inbox_groups as source of truth
        group_tag = get_group_tag_from_account(group_accounts[0]) if group_accounts else None
        if group_tag:
            ig = store.get_inbox_group_by_tag(group_tag)
            if ig:
                conflict = store.check_campaign_exclusivity(ig["id"], campaign_id)
                if conflict:
                    return {
                        "error": f"Group is already active in campaign {conflict['conflicting_campaign_id']}. Remove it first.",
                    }
```

- [ ] **Step 2: Update Supabase after successful assignment**

After the successful campaign assignment (around line 1919), update the inbox_group:

```python
                if group_tag and ig:
                    store.update_inbox_group(ig["id"],
                        campaign_ids=[campaign_id],
                        status="active",
                    )
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat: campaign exclusivity enforced via Supabase inbox_groups"
```

---

### Task 12: Enforce Group Size Standard

**Files:**
- Modify: `setup.py` (calculate_infra or new group creation logic)
- Modify: `dashboard.py` (new generic group creation endpoint)

- [ ] **Step 1: Add validation constant and check**

In `setup.py`, find the `calculate_infra` function. Add a constant near the top of the file:

```python
STANDARD_GROUP_DOMAINS = 14
STANDARD_GROUP_ACCOUNTS = 42  # 14 domains x 3 inboxes
```

- [ ] **Step 2: Add validation in the generic group creation flow**

In `dashboard.py`, find where new generic groups are created (around line 4197, the `/api/setup-pipeline/create` handler). Add a validation check:

```python
                    if body.get("type") == "generic":
                        domain_count = len(body.get("domains", []))
                        if domain_count != 14:
                            self._json_response({
                                "error": f"Generic groups must have exactly 14 domains (got {domain_count}). Standard: 14 domains / 42 accounts.",
                            }, status=400)
                            return
```

- [ ] **Step 3: Commit**

```bash
git add setup.py dashboard.py
git commit -m "feat: enforce 14 domain / 42 account standard for new generic groups"
```

---

### Task 13: Eliminate `b_group_assignments.json`

**Files:**
- Modify: `dashboard.py` (remove references at lines ~2652 and ~3707)

- [ ] **Step 1: Remove `b_group_assignments.json` from `api_rotation_status`**

In `dashboard.py` around line 3707, the rotation status function loads `b_group_assignments.json` to build labels. Replace this with tag-based lookup.

Old:
```python
    b_map_path = Path(__file__).parent / "clients" / "b_group_assignments.json"
    b_labels = {}
    if b_map_path.exists():
        with open(b_map_path) as f:
            b_map = json.load(f)
        for generic_name, info in b_map.items():
            b_labels[info["serves_client"]] = generic_name
```

New:
```python
    # B group labels now come from tags — no need for b_group_assignments.json
    b_labels = {}
    all_groups = store.get_all_inbox_groups()
    for g in all_groups:
        tag = g.get("group_tag", "")
        parsed = parse_group_tag(tag)
        if parsed["role"] == "client" and parsed["group_letter"] == "B":
            b_labels[parsed["client_name"]] = tag
```

- [ ] **Step 2: Remove from `api_acquisition_status` (around line 2652)**

Find the other reference to `b_group_assignments.json` and remove it. Replace any B-group exclusion logic with tag-based checks using `parse_group_tag`.

- [ ] **Step 3: Commit (do NOT delete the file yet — keep as backup until migration is verified)**

```bash
git add dashboard.py
git commit -m "refactor: remove b_group_assignments.json references, use tags"
```

---

### Task 14: Migration Script

**Files:**
- Create: `migrate_to_group_tags.py`

- [ ] **Step 1: Create the migration script**

```python
#!/usr/bin/env python3
"""One-time migration: update all SmartLead + Zapmail tags to the new group tag format.

Run once to migrate existing accounts. Safe to re-run (idempotent).

Usage: python3 migrate_to_group_tags.py [--dry-run]
"""

import json
import sys
import time
import requests
from pathlib import Path

# Load environment
sys.path.insert(0, str(Path(__file__).parent))
from setup import (
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API,
    sl_internal_headers,
)
from tag_utils import (
    get_group_tag_from_account, parse_group_tag,
    build_client_group_tag, build_acquisition_tag, ZAPMAIL_TAG_ID,
)
from zapmail_ops import zm_list_domains, zm_list_domain_tags, zm_create_domain_tag, zm_assign_domain_tag
import db as store

DRY_RUN = "--dry-run" in sys.argv

# Known acquisition group mapping: SmartLead client name -> letter
ACQ_CLIENT_MAP = {
    "A Group (250/day)": "A",
    "B Group (250/day)": "B",
    "C Group (250/day)": "C",
    "D Group (250/day)": "D",
    "E Group (250/day)": "E",
    "F Group (250/day)": "F",
    "G Group (250/day)": "G",
    "H Group (250/day)": "H",
    "I Group (250/day)": "I",
    "J Group (250/day)": "J",
    "K Group (250/day)": "K",
    "L Group (250/day)": "L",
    "Acquisition Inboxes": None,  # skip — legacy umbrella client
}


def get_all_sl_accounts():
    """Fetch all SmartLead accounts with tags."""
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100",
            timeout=30,
        )
        batch = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return accounts


def get_sl_clients():
    """Fetch all SmartLead clients."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    return r.json() if r.status_code == 200 else []


def migrate():
    print("Fetching SmartLead data...")
    all_accounts = get_all_sl_accounts()
    clients = get_sl_clients()
    all_tags = sl_get_all_tags()
    rotations = store.get_all_rotations()

    client_map = {c["id"]: c["name"] for c in clients}
    rotation_map = {r["client_name"]: r for r in rotations}

    # Build set of A vs B account IDs from rotation records
    a_account_ids = set()
    b_account_ids = set()
    for r in rotations:
        a_ids = r.get("group_a_ids", [])
        b_ids = r.get("group_b_ids", [])
        if isinstance(a_ids, str):
            a_ids = json.loads(a_ids)
        if isinstance(b_ids, str):
            b_ids = json.loads(b_ids)
        a_account_ids.update(a_ids)
        b_account_ids.update(b_ids)

    stats = {"client_migrated": 0, "acquisition_migrated": 0, "generic_ok": 0, "skipped": 0, "errors": 0}

    for acc in all_accounts:
        acc_id = acc["id"]
        email = acc.get("from_email", "")
        client_id = acc.get("client_id")
        client_name = client_map.get(client_id, "")
        current_group_tag = get_group_tag_from_account(acc)

        # Determine what the tag SHOULD be
        target_tag = None

        # Acquisition?
        if client_name in ACQ_CLIENT_MAP:
            letter = ACQ_CLIENT_MAP[client_name]
            if letter:
                target_tag = build_acquisition_tag(letter)
            else:
                stats["skipped"] += 1
                continue

        # Generic?
        elif client_name.lower().startswith("generic"):
            # Already correct if tag matches client name
            if current_group_tag and current_group_tag.lower() == client_name.lower():
                stats["generic_ok"] += 1
                continue
            target_tag = client_name  # Keep as-is (e.g., "Generic F")

        # Client group?
        elif client_name:
            # Determine A or B from rotation records
            if acc_id in b_account_ids:
                ab = "B"
            elif acc_id in a_account_ids:
                ab = "A"
            else:
                ab = "A"  # Default to A if not in rotation records
            target_tag = build_client_group_tag(client_name, ab)

        else:
            stats["skipped"] += 1
            continue

        # Check if already correct
        if current_group_tag == target_tag:
            if client_name.lower().startswith("generic"):
                stats["generic_ok"] += 1
            else:
                stats["skipped"] += 1
            continue

        # Apply the new tag
        print(f"  {email}: '{current_group_tag}' -> '{target_tag}'")

        if DRY_RUN:
            if "acquisition" in (target_tag or "").lower():
                stats["acquisition_migrated"] += 1
            else:
                stats["client_migrated"] += 1
            continue

        try:
            tag_id = sl_find_or_create_tag(target_tag, existing_tags=all_tags)
            # Rebuild full tag set: [Zapmail, group_tag, warmup_date]
            new_tag_ids = [ZAPMAIL_TAG_ID, tag_id]
            # Keep existing warmup date tag
            import re
            for t in acc.get("tags", []):
                if re.match(r'^\d{1,2}/\d{1,2}/\d{2}$', t.get("name", "")):
                    new_tag_ids.append(t["id"])
                    break

            sl_tag_account(acc_id, new_tag_ids, client_id=client_id)
            # Refresh all_tags cache in case we created a new tag
            all_tags[target_tag] = {"id": tag_id, "name": target_tag}

            if "acquisition" in target_tag.lower():
                stats["acquisition_migrated"] += 1
            else:
                stats["client_migrated"] += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"  ERROR on {email}: {e}")
            stats["errors"] += 1

    print(f"\nMigration {'(DRY RUN) ' if DRY_RUN else ''}complete:")
    print(f"  Client accounts migrated: {stats['client_migrated']}")
    print(f"  Acquisition accounts migrated: {stats['acquisition_migrated']}")
    print(f"  Generic already correct: {stats['generic_ok']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    migrate()
```

- [ ] **Step 2: Dry run the migration**

Run: `cd ~/email-infra && python3 migrate_to_group_tags.py --dry-run`

Review output. Verify the tag transitions look correct for each account type.

- [ ] **Step 3: Run the actual migration**

Run: `cd ~/email-infra && python3 migrate_to_group_tags.py`

Monitor for errors. Re-run if needed (idempotent).

- [ ] **Step 4: Update Supabase inbox_groups records**

After SmartLead tags are migrated, update the `group_tag` column in Supabase for all existing inbox_group rows. Add to the bottom of the migration script:

```python
    # Update inbox_groups Supabase records
    print("\nUpdating Supabase inbox_groups...")
    ig_groups = store.get_all_inbox_groups()
    for ig in ig_groups:
        old_name = ig.get("smartlead_client_name", "")
        assigned = ig.get("assigned_client")
        role = ig.get("role", "generic")

        if role == "generic" and not assigned:
            new_tag = old_name  # "Generic F" stays
        elif assigned:
            # Determine A/B from rotation
            rotation = rotation_map.get(assigned)
            ab = "A"
            if rotation:
                a_ids = rotation.get("group_a_ids", [])
                b_ids = rotation.get("group_b_ids", [])
                if isinstance(a_ids, str):
                    a_ids = json.loads(a_ids)
                if isinstance(b_ids, str):
                    b_ids = json.loads(b_ids)
                ig_account_ids = set(ig.get("account_ids") or [])
                if ig_account_ids & set(b_ids):
                    ab = "B"
            new_tag = build_client_group_tag(assigned, ab)
        else:
            new_tag = old_name

        if new_tag != ig.get("group_tag"):
            print(f"  inbox_group {ig['id']}: '{ig.get('group_tag')}' -> '{new_tag}'")
            if not DRY_RUN:
                store.update_inbox_group(ig["id"], group_tag=new_tag)
```

- [ ] **Step 5: Commit**

```bash
git add migrate_to_group_tags.py
git commit -m "feat: add one-time migration script for group tags"
```

---

### Task 15: Sync Dashboard to Vercel

**Files:**
- Copy: `dashboard.html` -> `web/public/index.html`

- [ ] **Step 1: Copy and deploy**

```bash
cp ~/email-infra/dashboard.html ~/email-infra/web/public/index.html
cd ~/email-infra/web && npx vercel --prod
```

- [ ] **Step 2: Commit**

```bash
cd ~/email-infra
git add web/public/index.html
git commit -m "deploy: sync dashboard.html to Vercel"
```

---

### Task 16: Delete `b_group_assignments.json` (After Verification)

**Files:**
- Delete: `clients/b_group_assignments.json`

- [ ] **Step 1: Verify the dashboard works without the file**

Load the dashboard, check:
- Client cards show A/B groups correctly
- Rotation status page works
- Acquisition tab works
- Sync check works
- Generic groups display correctly

- [ ] **Step 2: Delete the file**

```bash
git rm clients/b_group_assignments.json
git commit -m "cleanup: remove b_group_assignments.json, replaced by group tags"
```
