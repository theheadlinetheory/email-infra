# Domain Inventory Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show always-visible domain pool inventory badges in the dashboard topbar with Slack alerts when either pool drops below 20 available domains.

**Architecture:** Extend the existing `/api/domain-inventory` endpoint in `dashboard.py` to return both client and acquisition pool counts with threshold flags. Add a Slack alert function that debounces to once per pool per 24 hours. Frontend adds two badge elements to the topbar and an alert banner line for low inventory.

**Tech Stack:** Python (dashboard.py), vanilla JS/HTML (dashboard.html), Slack incoming webhook, Google Sheets via sheets.py

---

## File Structure

| File | Change | Responsibility |
|------|--------|---------------|
| `dashboard.py` | Modify | Update `api_domain_inventory()` to split pools, add Slack alert with debounce |
| `dashboard.html` | Modify | Add topbar badges + alert banner integration |
| `web/public/index.html` | Copy | Mirror of dashboard.html for Vercel |

---

### Task 1: Update `/api/domain-inventory` endpoint to split pools

**Files:**
- Modify: `dashboard.py` — `api_domain_inventory()` function (line ~1608-1624)

- [ ] **Step 1: Read the current `api_domain_inventory` function**

```bash
cd /Users/aidanhutchinson/email-infra
python3 -c "from sheets import get_available_domains, get_acquisition_domains; print('client:', len(get_available_domains())); print('acq:', len(get_acquisition_domains()))"
```

Verify both functions return domain lists.

- [ ] **Step 2: Update `api_domain_inventory()` to return both pools with thresholds**

In `dashboard.py`, replace the existing `api_domain_inventory()` function (around line 1608) with:

```python
DOMAIN_INVENTORY_THRESHOLD = 20

def api_domain_inventory():
    """Get available domain inventory split by client and acquisition pools."""
    client_domains = get_available_domains()
    acquisition_domains = get_acquisition_domains()
    client_count = len(client_domains)
    acq_count = len(acquisition_domains)
    client_low = client_count < DOMAIN_INVENTORY_THRESHOLD
    acq_low = acq_count < DOMAIN_INVENTORY_THRESHOLD

    # Send Slack alerts for low inventory (debounced)
    if client_low:
        _send_inventory_alert("Client", client_count)
    if acq_low:
        _send_inventory_alert("Acquisition", acq_count)

    return {
        "client_available": client_count,
        "acquisition_available": acq_count,
        "client_threshold": DOMAIN_INVENTORY_THRESHOLD,
        "acquisition_threshold": DOMAIN_INVENTORY_THRESHOLD,
        "client_low": client_low,
        "acquisition_low": acq_low,
    }
```

- [ ] **Step 3: Verify the endpoint returns correct data**

```bash
curl -s http://127.0.0.1:8099/api/domain-inventory | python3 -m json.tool
```

Expected: JSON with `client_available`, `acquisition_available`, `client_low`, `acquisition_low` fields.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: split domain-inventory endpoint into client/acquisition pools with thresholds"
```

---

### Task 2: Add Slack alert with 24-hour debounce

**Files:**
- Modify: `dashboard.py` — add `_send_inventory_alert()` function near `api_domain_inventory()`

- [ ] **Step 1: Add the debounced Slack alert function**

Add this right above `api_domain_inventory()` in `dashboard.py`:

```python
import requests
from datetime import datetime, timedelta

_inventory_alert_times = {}  # {"Client": datetime, "Acquisition": datetime}

def _send_inventory_alert(pool_name, count):
    """Send Slack alert for low domain inventory, max once per pool per 24h."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    now = datetime.utcnow()
    last_sent = _inventory_alert_times.get(pool_name)
    if last_sent and (now - last_sent) < timedelta(hours=24):
        return  # Already alerted recently

    try:
        requests.post(
            webhook_url,
            json={"text": f"⚠️ Domain inventory low: {pool_name} pool has {count} available (threshold: {DOMAIN_INVENTORY_THRESHOLD})"},
            timeout=10,
        )
        _inventory_alert_times[pool_name] = now
    except Exception as e:
        print(f"[SLACK] Failed to send inventory alert: {e}")
```

- [ ] **Step 2: Verify `requests` and `os` are already imported at top of dashboard.py**

```bash
head -30 /Users/aidanhutchinson/email-infra/dashboard.py | grep -E "^import|^from"
```

Both `requests` and `os` should already be imported. If `datetime` is not imported, add `from datetime import datetime, timedelta` to the imports.

- [ ] **Step 3: Test the Slack alert fires for low inventory**

If either pool is below 20, hitting the endpoint should send a Slack message:

```bash
curl -s http://127.0.0.1:8099/api/domain-inventory | python3 -m json.tool
```

Check the `#infra-alerts` Slack channel for the message. A second call within 24h should NOT send another alert.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: add Slack alerts for low domain inventory with 24h debounce"
```

---

### Task 3: Add topbar inventory badges to frontend

**Files:**
- Modify: `dashboard.html` — topbar HTML and `loadOverview()` function

- [ ] **Step 1: Add badge elements to the topbar**

In `dashboard.html`, find the topbar HTML (around line 230). Between the `topbar-nav` closing `</nav>` tag and the `topbar-right` opening div, there is a natural break. Instead, add the badges inside `topbar-left`, right after the `</nav>` tag:

Find:
```html
        </nav>
    </div>
```

Replace with:
```html
        </nav>
        <div id="inventory-badges" style="display:none;display:flex;gap:6px;align-items:center;margin-left:8px;">
            <span id="inv-client" class="badge badge-green" style="font-size:11px;cursor:default;"></span>
            <span id="inv-acq" class="badge badge-green" style="font-size:11px;cursor:default;"></span>
        </div>
    </div>
```

- [ ] **Step 2: Add inventory fetch to `loadOverview()`**

In the `loadOverview()` function, find the `Promise.all` block (around line 518) that fetches overview, unassigned, generic, and acquisition data. Add the inventory fetch:

Find:
```javascript
        const [overviewResp, unassignedResp, genericResp, acquisitionResp] = await Promise.all([
            fetch('/api/overview'),
            fetch('/api/unassigned').catch(() => null),
            fetch('/api/generic-groups').catch(() => null),
            fetch('/api/acquisition').catch(() => null),
        ]);
```

Replace with:
```javascript
        const [overviewResp, unassignedResp, genericResp, acquisitionResp, inventoryResp] = await Promise.all([
            fetch('/api/overview'),
            fetch('/api/unassigned').catch(() => null),
            fetch('/api/generic-groups').catch(() => null),
            fetch('/api/acquisition').catch(() => null),
            fetch('/api/domain-inventory').catch(() => null),
        ]);
```

- [ ] **Step 3: Parse inventory response and store it**

Find the block where sub-section responses are parsed (around line 537, comment `// Parse sub-section responses`). After the existing parsing of `unassignedData`, `genericData`, `acquisitionData`, add:

```javascript
        let inventoryData = null;
        if (inventoryResp && inventoryResp.ok) {
            try { inventoryData = await inventoryResp.json(); } catch(e) {}
        }
```

- [ ] **Step 4: Add inventory badge rendering in `renderOverview()`**

Find the line in `renderOverview()` that sets `last-updated` text (around line 591: `document.getElementById('last-updated').textContent = 'Updated: ' + time;`). Right after that line, add:

```javascript
    // Inventory badges
    if (inventoryData) {
        const invEl = document.getElementById('inventory-badges');
        invEl.style.display = 'flex';
        const clientBadge = document.getElementById('inv-client');
        const acqBadge = document.getElementById('inv-acq');
        clientBadge.textContent = 'Client: ' + inventoryData.client_available;
        clientBadge.className = 'badge ' + (inventoryData.client_low ? 'badge-red' : 'badge-green');
        acqBadge.textContent = 'Acq: ' + inventoryData.acquisition_available;
        acqBadge.className = 'badge ' + (inventoryData.acquisition_low ? 'badge-red' : 'badge-green');
    }
```

Note: `inventoryData` must be accessible inside `renderOverview()`. Since both `loadOverview()` and `renderOverview()` are in the same scope, either make `inventoryData` a module-level variable (like `overviewData`) or pass it. The simplest approach: declare `let inventoryData = null;` at the top of the script alongside `let overviewData = null;` and assign it in `loadOverview()`.

- [ ] **Step 5: Verify badges appear in topbar**

Restart the server and refresh `http://127.0.0.1:8099`. The topbar should show two badges between the nav tabs and the right side: `Client: N` and `Acq: N`, colored green or red based on the threshold.

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: add domain inventory badges to dashboard topbar"
```

---

### Task 4: Add low inventory warnings to alert banner

**Files:**
- Modify: `dashboard.html` — `renderOverview()` alert banner section

- [ ] **Step 1: Add inventory alerts to the alert banner**

In `renderOverview()`, find the alert banner logic (around line 596). The banner currently checks for `blocked_accounts`, `smtp_failures`, `attentionClients`, and `idle_inboxes`. Add inventory checks to the condition and the banner content.

Find the `if` condition that determines whether to show the alert banner:
```javascript
    if (d.blocked_accounts.length > 0 || d.smtp_failures > 0 || attentionClients.length > 0 || d.idle_inboxes > 0) {
```

Replace with:
```javascript
    const invLow = inventoryData && (inventoryData.client_low || inventoryData.acquisition_low);
    if (d.blocked_accounts.length > 0 || d.smtp_failures > 0 || attentionClients.length > 0 || d.idle_inboxes > 0 || invLow) {
```

Then, right after the `<h3>Alerts</h3>` line inside the banner HTML building, before the `idle_inboxes` check, add:

```javascript
        if (inventoryData && inventoryData.client_low) {
            html += '<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Client pool has ' + inventoryData.client_available + ' available (need 20+)</div>';
        }
        if (inventoryData && inventoryData.acquisition_low) {
            html += '<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Acquisition pool has ' + inventoryData.acquisition_available + ' available (need 20+)</div>';
        }
```

- [ ] **Step 2: Verify alert banner shows inventory warnings**

Restart server and refresh. If either pool is below 20, the red alert banner at the top should include the inventory warning line.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: add low domain inventory warnings to alert banner"
```

---

### Task 5: Sync to Vercel and final verification

**Files:**
- Copy: `dashboard.html` → `web/public/index.html`

- [ ] **Step 1: Copy dashboard to Vercel directory**

```bash
cp /Users/aidanhutchinson/email-infra/dashboard.html /Users/aidanhutchinson/email-infra/web/public/index.html
```

- [ ] **Step 2: Commit the Vercel copy**

```bash
git add web/public/index.html
git commit -m "chore: sync dashboard.html to Vercel public dir"
```

- [ ] **Step 3: Deploy to Vercel**

```bash
cd /Users/aidanhutchinson/email-infra/web && npx vercel --prod
```

- [ ] **Step 4: Restart local server and verify everything works**

Kill the existing server process and restart:

```bash
kill $(lsof -ti :8099) 2>/dev/null; sleep 1
cd /Users/aidanhutchinson/email-infra && python3 dashboard.py &
sleep 3
curl -s http://127.0.0.1:8099/api/domain-inventory | python3 -m json.tool
```

Verify:
- Topbar badges show correct counts with appropriate colors
- Alert banner shows warnings if either pool < 20
- Slack alert fires (check `#infra-alerts` channel) if pool is low
- Second refresh does NOT send duplicate Slack alert (24h debounce)
