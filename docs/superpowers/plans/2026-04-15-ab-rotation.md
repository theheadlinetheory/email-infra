# A/B Group Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add A/B group rotation so each fulfillment client can swap between two sets of email accounts monthly, starting with Kay's Landscaping as the test case.

**Architecture:** New `client_rotations` Supabase table stores each client's Group A and B account IDs plus the active group. A swap function finds campaigns containing the outgoing group's accounts, adds the incoming group, and removes the outgoing group. Dashboard gets a Rotation section with per-client swap buttons.

**Tech Stack:** Python (dashboard.py, db.py), vanilla JS/HTML (dashboard.html), Supabase PostgreSQL, SmartLead API v1

---

## File Structure

| File | Change | Responsibility |
|------|--------|---------------|
| `supabase_schema.sql` | Modify | Add `client_rotations` table DDL |
| `db.py` | Modify | Add CRUD functions for `client_rotations` table |
| `dashboard.py` | Modify | Add rotation API endpoints and swap logic |
| `dashboard.html` | Modify | Add Rotation section to SmartLead tab |
| `web/public/index.html` | Copy | Mirror of dashboard.html for Vercel |

---

### Task 1: Create `client_rotations` Supabase table

**Files:**
- Modify: `supabase_schema.sql` (line ~28, after `pending_deletions` index)

- [ ] **Step 1: Add table DDL to schema file**

In `supabase_schema.sql`, after line 28 (`create index if not exists idx_pending_domain on pending_deletions (domain);`), add:

```sql
-- A/B rotation state per client
create table if not exists client_rotations (
    client_name text primary key,
    group_a_ids jsonb not null default '[]',
    group_b_ids jsonb not null default '[]',
    active_group text not null default 'A',
    last_swap_date text not null default '',
    created_at timestamptz not null default now()
);
```

Also add after the existing `alter table` block (line ~59):

```sql
alter table client_rotations disable row level security;
```

- [ ] **Step 2: Run the DDL against live Supabase**

```bash
cd /Users/aidanhutchinson/email-infra
supabase db query "create table if not exists client_rotations (client_name text primary key, group_a_ids jsonb not null default '[]', group_b_ids jsonb not null default '[]', active_group text not null default 'A', last_swap_date text not null default '', created_at timestamptz not null default now()); alter table client_rotations disable row level security;" --linked
```

Expected: Empty rows result (DDL success).

- [ ] **Step 3: Verify table exists**

```bash
supabase db query "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'client_rotations' ORDER BY ordinal_position;" --linked
```

Expected: 6 columns (client_name, group_a_ids, group_b_ids, active_group, last_swap_date, created_at).

- [ ] **Step 4: Commit**

```bash
git add supabase_schema.sql
git commit -m "feat: add client_rotations table for A/B group rotation"
```

---

### Task 2: Add `client_rotations` CRUD to `db.py`

**Files:**
- Modify: `db.py` (after the pending_deletions section, around line 133)

- [ ] **Step 1: Add rotation CRUD functions**

In `db.py`, after `remove_pending_deletion()` (line 132) and before the `# Client Configs` section (line 135), add:

```python
# ---------------------------------------------------------------------------
# Client Rotations (A/B group swap)
# ---------------------------------------------------------------------------

def get_all_rotations() -> list[dict]:
    """Get all client rotation records."""
    return _request("GET", "/client_rotations", params={"select": "*", "order": "client_name"})


def get_rotation(client_name: str) -> dict | None:
    """Get a single client's rotation record."""
    rows = _request("GET", "/client_rotations", params={
        "select": "*",
        "client_name": f"eq.{client_name}",
    })
    return rows[0] if rows else None


def upsert_rotation(client_name: str, group_a_ids: list, group_b_ids: list,
                    active_group: str = "A", last_swap_date: str = "") -> None:
    """Create or update a rotation record."""
    row = {
        "client_name": client_name,
        "group_a_ids": json.dumps(group_a_ids),
        "group_b_ids": json.dumps(group_b_ids),
        "active_group": active_group,
        "last_swap_date": last_swap_date,
    }
    _request("POST", "/client_rotations", json_body=row, headers={
        "Prefer": "resolution=merge-duplicates",
    })


def update_rotation_swap(client_name: str, new_active: str, swap_date: str) -> None:
    """Flip the active group after a swap."""
    _request("PATCH", "/client_rotations", params={
        "client_name": f"eq.{client_name}",
    }, json_body={
        "active_group": new_active,
        "last_swap_date": swap_date,
    })
```

- [ ] **Step 2: Verify functions work**

```bash
cd /Users/aidanhutchinson/email-infra
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')
from db import upsert_rotation, get_rotation, get_all_rotations, update_rotation_swap

# Insert test
upsert_rotation('__test__', [1,2,3], [4,5,6], 'A', '')
r = get_rotation('__test__')
print('Get:', r)
assert r['active_group'] == 'A'

# Swap test
update_rotation_swap('__test__', 'B', '2026-04-15')
r = get_rotation('__test__')
print('After swap:', r)
assert r['active_group'] == 'B'

# Cleanup
from db import _request
_request('DELETE', '/client_rotations', params={'client_name': 'eq.__test__'})
print('All rotations:', get_all_rotations())
print('PASS')
"
```

Expected: `PASS` with correct get/swap results.

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat: add client_rotations CRUD to db.py"
```

---

### Task 3: Add swap logic and API endpoints to `dashboard.py`

**Files:**
- Modify: `dashboard.py` — add `swap_client_group()` function and 3 API endpoints

- [ ] **Step 1: Add the swap function**

In `dashboard.py`, add this function before the `DashboardHandler` class (before line 2564):

```python
# ---------------------------------------------------------------------------
# A/B Rotation
# ---------------------------------------------------------------------------

def swap_client_group(client_name):
    """Swap a client's active group in all their campaigns.

    1. Read rotation record
    2. Find campaigns with outgoing accounts
    3. Add incoming accounts, remove outgoing accounts
    4. Update rotation record
    Returns dict with results.
    """
    rotation = store.get_rotation(client_name)
    if not rotation:
        return {"error": f"No rotation record for '{client_name}'"}

    active = rotation["active_group"]
    a_ids = rotation["group_a_ids"]
    b_ids = rotation["group_b_ids"]
    if isinstance(a_ids, str):
        a_ids = json.loads(a_ids)
    if isinstance(b_ids, str):
        b_ids = json.loads(b_ids)

    if active == "A":
        outgoing_ids, incoming_ids, new_active = set(a_ids), b_ids, "B"
    else:
        outgoing_ids, incoming_ids, new_active = set(b_ids), a_ids, "A"

    if not incoming_ids:
        return {"error": f"Group {new_active} has no accounts — cannot swap"}

    # Find all campaigns
    r = requests.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
    all_campaigns = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    active_campaigns = [c for c in all_campaigns if c.get("status") in ("ACTIVE", "PAUSED")]

    # Find campaigns containing outgoing accounts
    campaigns_updated = []
    for camp in active_campaigns:
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
        return {"error": f"No campaigns found with Group {active} accounts for {client_name}"}

    # Add incoming accounts to campaigns
    for camp in campaigns_updated:
        requests.post(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": incoming_ids},
            timeout=30,
        )
        time.sleep(0.3)

    # Remove outgoing accounts from campaigns
    for camp in campaigns_updated:
        for old_id in outgoing_ids:
            requests.delete(
                f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts/{old_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            time.sleep(0.3)

    # Update rotation record
    today = datetime.now().strftime("%Y-%m-%d")
    store.update_rotation_swap(client_name, new_active, today)

    return {
        "ok": True,
        "client_name": client_name,
        "previous_group": active,
        "new_group": new_active,
        "campaigns_updated": len(campaigns_updated),
        "campaign_names": [c["name"] for c in campaigns_updated],
        "accounts_added": len(incoming_ids),
        "accounts_removed": len(outgoing_ids),
    }


def api_rotation_status():
    """GET /api/rotation/status — return all rotation records."""
    rotations = store.get_all_rotations()
    for r in rotations:
        if isinstance(r.get("group_a_ids"), str):
            r["group_a_ids"] = json.loads(r["group_a_ids"])
        if isinstance(r.get("group_b_ids"), str):
            r["group_b_ids"] = json.loads(r["group_b_ids"])
    return {"rotations": rotations}
```

- [ ] **Step 2: Wire up GET endpoint in `do_GET`**

In `dashboard.py`, inside `do_GET()`, find (around line 2646):

```python
                elif path == "/api/debug/supabase":
                    self._json_response(api_debug_supabase())
```

Add before it:

```python
                elif path == "/api/rotation/status":
                    self._json_response(api_rotation_status())
```

- [ ] **Step 3: Wire up POST endpoints in `do_POST`**

In `dashboard.py`, inside `do_POST()`, find the end of the route chain (the last `elif` before the fallback). Add:

```python
        elif path == "/api/rotation/swap":
            client_name = body.get("client_name", "")
            if not client_name:
                self._error(400, "client_name required")
                return
            result = swap_client_group(client_name)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/rotation/swap-all":
            rotations = store.get_all_rotations()
            results = []
            for rot in rotations:
                result = swap_client_group(rot["client_name"])
                results.append(result)
            self._json_response({"results": results})
```

- [ ] **Step 4: Verify the API endpoint works**

```bash
curl -s http://127.0.0.1:8099/api/rotation/status | python3 -m json.tool
```

Expected: `{"rotations": []}` (empty until we populate Kay's).

- [ ] **Step 5: Commit**

```bash
git add dashboard.py
git commit -m "feat: add A/B rotation swap logic and API endpoints"
```

---

### Task 4: Add Rotation section to dashboard UI

**Files:**
- Modify: `dashboard.html` — add Rotation section inside the SmartLead tab

- [ ] **Step 1: Add Rotation HTML section**

In `dashboard.html`, find the closing `</div>` of `tab-smartlead` (line 312, the `</div>` right before `<!-- ZapMail Tab -->`). Insert before it:

```html
            <div id="rotation-section" style="margin-top:28px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <h2 class="section-title" style="margin:0;">A/B Rotation</h2>
                    <button class="action-btn primary" onclick="swapAll()" id="swap-all-btn" style="font-size:12px;">Swap All Clients</button>
                </div>
                <div id="rotation-grid" class="clients-grid"></div>
            </div>
```

- [ ] **Step 2: Add rotation fetch to `loadOverview()`**

In `dashboard.html`, find the `Promise.all` in `loadOverview()` that fetches overview, unassigned, generic, acquisition, and inventory data. Add `fetch('/api/rotation/status').catch(() => null)` to the array, and destructure the result as `rotationResp`.

Then after the existing response parsing block, add:

```javascript
        let rotationData = null;
        if (rotationResp && rotationResp.ok) {
            try { rotationData = await rotationResp.json(); } catch(e) {}
        }
```

- [ ] **Step 3: Add `renderRotation()` function**

Add this function in the `<script>` section, after the other render functions:

```javascript
    function renderRotation(data) {
        const grid = document.getElementById('rotation-grid');
        const section = document.getElementById('rotation-section');
        if (!data || !data.rotations || data.rotations.length === 0) {
            section.style.display = 'none';
            return;
        }
        section.style.display = 'block';
        let html = '';
        for (const rot of data.rotations) {
            const aCount = (rot.group_a_ids || []).length;
            const bCount = (rot.group_b_ids || []).length;
            const active = rot.active_group || 'A';
            const lastSwap = rot.last_swap_date || 'Never';
            const aBadge = active === 'A' ? 'badge-green' : 'badge-muted';
            const bBadge = active === 'B' ? 'badge-green' : 'badge-muted';
            html += '<div class="client-card">';
            html += '<div class="client-header">';
            html += '<h3 class="client-name">' + rot.client_name + '</h3>';
            html += '<span class="badge ' + (active === 'A' ? 'badge-green' : 'badge-blue') + '">Group ' + active + ' Active</span>';
            html += '</div>';
            html += '<div class="client-stats">';
            html += '<div class="stat"><span class="stat-value ' + aBadge + '">' + aCount + '</span><span class="stat-label">Group A</span></div>';
            html += '<div class="stat"><span class="stat-value ' + bBadge + '">' + bCount + '</span><span class="stat-label">Group B</span></div>';
            html += '<div class="stat"><span class="stat-value">' + lastSwap + '</span><span class="stat-label">Last Swap</span></div>';
            html += '</div>';
            html += '<div style="margin-top:8px;text-align:right;">';
            html += '<button class="action-btn secondary" style="font-size:11px;" onclick="swapClient(\'' + rot.client_name.replace(/'/g, "\\'") + '\')">Swap to Group ' + (active === 'A' ? 'B' : 'A') + '</button>';
            html += '</div>';
            html += '</div>';
        }
        grid.innerHTML = html;
    }
```

- [ ] **Step 4: Add swap action functions**

Add these functions in the `<script>` section:

```javascript
    async function swapClient(clientName) {
        if (!confirm('Swap ' + clientName + ' to the other group? This will update all their campaigns.')) return;
        const btn = event.target;
        btn.disabled = true;
        btn.textContent = 'Swapping...';
        try {
            const resp = await fetch('/api/rotation/swap', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({client_name: clientName}),
            });
            const result = await resp.json();
            if (result.ok) {
                alert('Swapped ' + clientName + ' to Group ' + result.new_group + '.\n' + result.campaigns_updated + ' campaigns updated.');
                loadOverview();
            } else {
                alert('Swap failed: ' + (result.error || 'Unknown error'));
            }
        } catch(e) {
            alert('Swap error: ' + e.message);
        }
        btn.disabled = false;
    }

    async function swapAll() {
        if (!confirm('Swap ALL clients to their other group? This affects all campaigns.')) return;
        const btn = document.getElementById('swap-all-btn');
        btn.disabled = true;
        btn.textContent = 'Swapping All...';
        try {
            const resp = await fetch('/api/rotation/swap-all', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({}),
            });
            const result = await resp.json();
            const ok = (result.results || []).filter(r => r.ok).length;
            const fail = (result.results || []).filter(r => r.error).length;
            alert('Swap All complete: ' + ok + ' succeeded, ' + fail + ' failed.');
            loadOverview();
        } catch(e) {
            alert('Swap All error: ' + e.message);
        }
        btn.disabled = false;
        btn.textContent = 'Swap All Clients';
    }
```

- [ ] **Step 5: Call `renderRotation()` from `renderOverview()`**

In the `renderOverview()` function, after the existing rendering calls, add:

```javascript
    renderRotation(rotationData);
```

Note: `rotationData` must be accessible inside `renderOverview()`. Declare `let rotationData = null;` at the top of the script alongside `overviewData` and assign it in `loadOverview()`.

- [ ] **Step 6: Verify UI renders**

Restart server, refresh dashboard. The Rotation section should either be hidden (no records) or show Kay's data once populated.

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: add A/B rotation UI section to dashboard"
```

---

### Task 5: Populate Kay's Landscaping rotation record for testing

**Files:**
- No file changes — this is a data population step using the API and a one-time script

- [ ] **Step 1: Identify Kay's Group A accounts (currently in campaigns)**

```bash
cd /Users/aidanhutchinson/email-infra
python3 << 'PYEOF'
import sys, os, json, requests, time
from dotenv import load_dotenv
load_dotenv('.env')

SMARTLEAD_API = 'https://server.smartlead.ai/api/v1'
SMARTLEAD_KEY = os.environ.get('SMARTLEAD_API_KEY', '')

# Get Kay's client ID
clients = requests.get(f'{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}', timeout=30).json()
kays_id = None
for c in clients:
    if "kay" in c['name'].lower():
        kays_id = c['id']
        print(f"Kay's client ID: {kays_id} ({c['name']})")
        break

# Get all Kay's accounts
all_accounts = []
offset = 0
while True:
    batch = requests.get(f'{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100', timeout=30).json()
    if isinstance(batch, list):
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    else:
        break

kays_accounts = [a for a in all_accounts if a.get('client_id') == kays_id]
print(f"Kay's total accounts: {len(kays_accounts)}")

# Get all campaigns and find which Kay's accounts are in them
campaigns = requests.get(f'{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}', timeout=30).json()
active_camps = [c for c in campaigns if c.get('status') in ('ACTIVE', 'PAUSED')]

in_campaign_ids = set()
for camp in active_camps:
    cr = requests.get(f'{SMARTLEAD_API}/campaigns/{camp["id"]}/email-accounts?api_key={SMARTLEAD_KEY}', timeout=30)
    if cr.status_code == 200:
        camp_accts = cr.json() if isinstance(cr.json(), list) else []
        kays_in_camp = [a['id'] for a in camp_accts if a['id'] in {x['id'] for x in kays_accounts}]
        if kays_in_camp:
            in_campaign_ids.update(kays_in_camp)
            print(f"  Campaign '{camp.get('name','?')}': {len(kays_in_camp)} Kay's accounts")
    time.sleep(0.3)

group_a = sorted(in_campaign_ids)
group_b = sorted(set(a['id'] for a in kays_accounts) - in_campaign_ids)

print(f"\nGroup A (in campaigns): {len(group_a)} accounts")
print(f"Group B (warmed, not in campaigns): {len(group_b)} accounts")
print(f"\nGROUP_A_IDS = {json.dumps(group_a)}")
print(f"GROUP_B_IDS = {json.dumps(group_b)}")
PYEOF
```

Review the output. Group A should be the accounts currently in campaigns. Group B should be the remaining warmed accounts.

- [ ] **Step 2: Create the rotation record**

Using the IDs from Step 1, run:

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')
from db import upsert_rotation, get_rotation

# PASTE THE IDS FROM STEP 1 OUTPUT
GROUP_A_IDS = []  # <-- paste from step 1
GROUP_B_IDS = []  # <-- paste from step 1

upsert_rotation(\"Kay's Landscaping\", GROUP_A_IDS, GROUP_B_IDS, 'A', '')
r = get_rotation(\"Kay's Landscaping\")
print(f'Created rotation: active={r[\"active_group\"]}, A={len(GROUP_A_IDS)}, B={len(GROUP_B_IDS)}')
"
```

- [ ] **Step 3: Verify on dashboard**

Refresh the dashboard. The Rotation section should appear with Kay's Landscaping showing Group A active, correct account counts, and a "Swap to Group B" button.

- [ ] **Step 4: Test swap during off-hours**

Click the "Swap to Group B" button on the dashboard (or run via curl):

```bash
curl -s -X POST http://127.0.0.1:8099/api/rotation/swap \
  -H "Content-Type: application/json" \
  -d '{"client_name": "Kay'\''s Landscaping"}' | python3 -m json.tool
```

Expected: `ok: true` with campaigns_updated count and new_group "B".

- [ ] **Step 5: Verify the swap worked in SmartLead**

Manually check one of the updated campaigns in SmartLead to confirm:
- Group B accounts are now in the campaign
- Group A accounts are removed

- [ ] **Step 6: Test swap back**

Run the swap again to flip back to Group A:

```bash
curl -s -X POST http://127.0.0.1:8099/api/rotation/swap \
  -H "Content-Type: application/json" \
  -d '{"client_name": "Kay'\''s Landscaping"}' | python3 -m json.tool
```

Expected: `ok: true` with new_group "A". Verify in SmartLead that Group A is back in campaigns.

---

### Task 6: Sync dashboard to Vercel

**Files:**
- Copy: `dashboard.html` → `web/public/index.html`

- [ ] **Step 1: Copy and deploy**

```bash
cp /Users/aidanhutchinson/email-infra/dashboard.html /Users/aidanhutchinson/email-infra/web/public/index.html
```

- [ ] **Step 2: Commit**

```bash
git add web/public/index.html
git commit -m "chore: sync dashboard.html to Vercel public dir"
```

- [ ] **Step 3: Deploy**

```bash
cd /Users/aidanhutchinson/email-infra/web && npx vercel --prod
```
