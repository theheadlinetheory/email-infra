# Domain Aging Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a domain aging pool tracker to the infra dashboard so batches of pre-aged domains are tracked systematically with a UI in the Domains tab.

**Architecture:** Backend reads/writes `state/aging_pool.json`, exposes 3 API endpoints (GET pool, POST add batch, POST activate). Frontend adds an "Aging Pool" section at the top of the Domains tab with summary cards, per-batch progress bars, expandable domain lists, and an add-batch modal.

**Tech Stack:** Python (dashboard.py HTTP handler), vanilla JS (secondary-tabs.js), existing CSS variables from dashboard.css.

**Spec:** `docs/superpowers/specs/2026-05-09-domain-aging-tracker-design.md`

---

### Task 1: Backend — `api_aging_pool()` GET endpoint

**Files:**
- Modify: `dashboard.py` — add `api_aging_pool()` function (~line 2248, after `api_domain_inventory`)
- Modify: `dashboard.py` — add GET route in `do_GET` (~line 3526, before the final `else`)

- [ ] **Step 1: Add the `api_aging_pool()` function**

Insert after the `api_domain_inventory()` function (after line 2248):

```python
def api_aging_pool():
    """Aging domain pool — batches of domains waiting to build reputation."""
    pool_path = Path(__file__).parent / "state" / "aging_pool.json"
    old_path = Path(__file__).parent / "state" / "generic_aging_domains.json"

    if not pool_path.exists() and old_path.exists():
        with open(old_path) as f:
            old = json.load(f)
        pool = {
            "threshold_days": 30,
            "batches": [{
                "id": f"{old.get('purchased', '2026-05-08')}-info-{len(old.get('domains', []))}",
                "name": old.get("description", "Imported batch"),
                "purchased": old.get("purchased", "2026-05-08"),
                "cost": 231.70,
                "ns_provider": "CloudNS",
                "status": "aging",
                "domains": old.get("domains", []),
            }],
        }
        with open(pool_path, "w") as f:
            json.dump(pool, f, indent=2)

    if not pool_path.exists():
        return {"threshold_days": 30, "total_domains": 0, "total_ready": 0, "total_b_groups_possible": 0, "batches": []}

    with open(pool_path) as f:
        pool = json.load(f)

    threshold = pool.get("threshold_days", 30)
    now = datetime.now()
    total_domains = 0
    total_ready = 0

    for batch in pool.get("batches", []):
        if batch.get("status") == "activated":
            batch["domain_count"] = 0
            batch["days_aged"] = 0
            batch["days_remaining"] = 0
            batch["progress_pct"] = 100
            batch["ready"] = True
            batch["b_groups_possible"] = 0
            continue

        count = len(batch.get("domains", []))
        batch["domain_count"] = count
        total_domains += count

        try:
            purchased = datetime.strptime(batch["purchased"], "%Y-%m-%d")
            days_aged = (now - purchased).days
        except (KeyError, ValueError):
            days_aged = 0

        batch["days_aged"] = days_aged
        batch["days_remaining"] = max(0, threshold - days_aged)
        batch["progress_pct"] = min(100, round(days_aged / threshold * 100)) if threshold > 0 else 100
        batch["ready"] = days_aged >= threshold
        batch["b_groups_possible"] = count // 14

        if batch["ready"]:
            total_ready += count

    return {
        "threshold_days": threshold,
        "total_domains": total_domains,
        "total_ready": total_ready,
        "total_b_groups_possible": total_domains // 14,
        "batches": pool.get("batches", []),
    }
```

- [ ] **Step 2: Add the GET route**

In `do_GET`, insert before the final `else` block (before line 3526):

```python
                elif path == "/api/aging-pool":
                    self._json_response(api_aging_pool())
```

- [ ] **Step 3: Test the endpoint**

Run: `cd ~/email-infra && python3 -c "from dashboard import api_aging_pool; import json; print(json.dumps(api_aging_pool(), indent=2))"`

Expected: JSON output with the migrated batch from `generic_aging_domains.json`, showing 70 domains, `days_aged` of 1, `days_remaining` of 29, `ready: false`.

- [ ] **Step 4: Verify `state/aging_pool.json` was created**

Run: `cat ~/email-infra/state/aging_pool.json | python3 -m json.tool | head -15`

Expected: File exists with one batch containing the 70 domains.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py state/aging_pool.json
git commit -m "feat: add aging pool GET endpoint with migration from generic_aging_domains.json"
```

---

### Task 2: Backend — `api_aging_pool_add()` and `api_aging_pool_activate()` POST endpoints

**Files:**
- Modify: `dashboard.py` — add two functions after `api_aging_pool()`
- Modify: `dashboard.py` — add two POST routes in `do_POST` (before the final `else` at line 3786)

- [ ] **Step 1: Add `api_aging_pool_add()` function**

Insert directly after `api_aging_pool()`:

```python
def api_aging_pool_add(body):
    """Add a new batch of aging domains."""
    domains = body.get("domains", [])
    name = body.get("name", "").strip()
    purchased = body.get("purchased", "")
    if not domains or not name or not purchased:
        return {"error": "name, purchased, and domains required"}

    pool_path = Path(__file__).parent / "state" / "aging_pool.json"
    if pool_path.exists():
        with open(pool_path) as f:
            pool = json.load(f)
    else:
        pool = {"threshold_days": 30, "batches": []}

    batch_id = f"{purchased}-{len(domains)}"
    batch = {
        "id": batch_id,
        "name": name,
        "purchased": purchased,
        "cost": body.get("cost", 0),
        "ns_provider": body.get("ns_provider", "CloudNS"),
        "status": "aging",
        "domains": [d.strip() for d in domains if d.strip()],
    }
    pool["batches"].append(batch)

    with open(pool_path, "w") as f:
        json.dump(pool, f, indent=2)

    return {"ok": True, "batch_id": batch_id, "domain_count": len(batch["domains"])}
```

- [ ] **Step 2: Add `api_aging_pool_activate()` function**

Insert directly after `api_aging_pool_add()`:

```python
def api_aging_pool_activate(body):
    """Remove domains from an aging batch for activation."""
    batch_id = body.get("batch_id", "")
    count = body.get("count", 14)
    if not batch_id:
        return {"error": "batch_id required"}

    pool_path = Path(__file__).parent / "state" / "aging_pool.json"
    if not pool_path.exists():
        return {"error": "no aging pool found"}

    with open(pool_path) as f:
        pool = json.load(f)

    batch = None
    for b in pool.get("batches", []):
        if b["id"] == batch_id:
            batch = b
            break
    if not batch:
        return {"error": f"batch {batch_id} not found"}
    if batch.get("status") == "activated":
        return {"error": "batch already fully activated"}

    available = batch.get("domains", [])
    to_activate = available[:count]
    batch["domains"] = available[count:]
    if not batch["domains"]:
        batch["status"] = "activated"

    with open(pool_path, "w") as f:
        json.dump(pool, f, indent=2)

    return {"ok": True, "activated_domains": to_activate, "remaining": len(batch["domains"]), "batch_status": batch["status"]}
```

- [ ] **Step 3: Add the POST routes**

In `do_POST`, insert before the final `else` block (before `self._error(404, "Not found")`):

```python
        elif path == "/api/aging-pool/add":
            result = api_aging_pool_add(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/aging-pool/activate":
            result = api_aging_pool_activate(body)
            self._json_response(result, 400 if "error" in result else 200)
```

- [ ] **Step 4: Test add endpoint**

Run: `cd ~/email-infra && python3 -c "from dashboard import api_aging_pool_add; import json; print(json.dumps(api_aging_pool_add({'name':'Test','purchased':'2026-05-09','domains':['test1.info','test2.info']}), indent=2))"`

Expected: `{"ok": true, "batch_id": "2026-05-09-2", "domain_count": 2}`

- [ ] **Step 5: Remove test batch and commit**

Run: `cd ~/email-infra && python3 -c "
import json
with open('state/aging_pool.json') as f: pool = json.load(f)
pool['batches'] = [b for b in pool['batches'] if b['id'] != '2026-05-09-2']
with open('state/aging_pool.json','w') as f: json.dump(pool, f, indent=2)
print('Cleaned up test batch')
"`

```bash
git add dashboard.py
git commit -m "feat: add aging pool add and activate POST endpoints"
```

---

### Task 3: Frontend — Aging Pool section in Domains tab

**Files:**
- Modify: `dashboard.html` — add `div#aging-pool-section` inside `tab-domains` (line 158, before `dom-summary-row`)
- Modify: `js/secondary-tabs.js` — add `loadAgingPool()`, `renderAgingPool()` functions and call from `loadDomains()`

- [ ] **Step 1: Add the HTML container**

In `dashboard.html`, insert after the `tab-domains` opening div (after line 158) and before `dom-summary-row`:

```html
            <div id="aging-pool-section" style="margin-bottom:24px;"></div>
```

- [ ] **Step 2: Add `loadAgingPool()` and `renderAgingPool()` in `secondary-tabs.js`**

Insert at the very top of `secondary-tabs.js` (before line 1), adding a new section:

```javascript
// ── Aging Pool ──

var agingPoolData = null;

async function loadAgingPool() {
    try {
        var resp = await fetch('/api/aging-pool');
        agingPoolData = await resp.json();
        renderAgingPool();
    } catch (err) {
        document.getElementById('aging-pool-section').innerHTML = '';
    }
}

function renderAgingPool() {
    var d = agingPoolData;
    if (!d || !d.batches || d.batches.length === 0) {
        document.getElementById('aging-pool-section').innerHTML = '';
        return;
    }

    var activeBatches = d.batches.filter(function(b) { return b.status !== 'activated'; });
    if (activeBatches.length === 0) {
        document.getElementById('aging-pool-section').innerHTML = '';
        return;
    }

    var nearestDays = Math.min.apply(null, activeBatches.map(function(b) { return b.days_remaining; }));

    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">' +
        '<h2 class="section-title" style="margin:0;">Aging Pool</h2>' +
        '<button class="action-btn secondary" onclick="showAddBatchModal()" style="font-size:12px;">+ Add Batch</button>' +
        '</div>';

    html += '<div class="summary-row">' +
        statCard(d.total_domains, 'Domains Aging', 'good') +
        statCard(d.total_b_groups_possible, 'Future B Groups', 'good') +
        statCard(d.total_ready > 0 ? d.total_ready + ' ready' : nearestDays + 'd left', d.total_ready > 0 ? 'Ready to Activate' : 'Until Ready', d.total_ready > 0 ? 'good' : 'warn') +
        '</div>';

    activeBatches.forEach(function(batch) {
        var progressColor = batch.ready ? '#22c55e' : '#f59e0b';
        var statusBadge = batch.ready
            ? '<span class="badge badge-green">Ready</span>'
            : '<span class="badge badge-yellow">Aging</span>';
        var cardId = 'aging-' + batch.id.replace(/[^a-zA-Z0-9]/g, '_');

        html += '<div class="client-card" style="margin-bottom:12px;">' +
            '<div class="client-header" onclick="document.getElementById(\'' + cardId + '\').style.display = document.getElementById(\'' + cardId + '\').style.display === \'none\' ? \'block\' : \'none\'">' +
            '<div style="display:flex;align-items:center;gap:12px;">' +
            '<h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text-primary);">' + batch.name + '</h3>' +
            statusBadge +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:16px;font-size:13px;color:var(--text-muted);">' +
            '<span>' + batch.domain_count + ' domains</span>' +
            '<span>$' + (batch.cost || 0).toFixed(2) + '</span>' +
            '<span>Purchased ' + batch.purchased + '</span>' +
            '<span>' + batch.days_aged + ' / ' + d.threshold_days + ' days</span>' +
            '</div>' +
            '</div>' +
            '<div style="margin:8px 16px 12px;background:var(--bg-input);border-radius:6px;height:8px;overflow:hidden;">' +
            '<div style="height:100%;width:' + batch.progress_pct + '%;background:' + progressColor + ';border-radius:6px;transition:width .3s;"></div>' +
            '</div>';

        if (batch.ready) {
            html += '<div style="padding:0 16px 12px;display:flex;gap:8px;">' +
                '<button class="action-btn primary" onclick="activateAgingBatch(\'' + batch.id + '\', 14)" style="font-size:12px;">Activate 14 (1 B Group)</button>' +
                '</div>';
        }

        html += '<div id="' + cardId + '" style="display:none;padding:0 16px 12px;">' +
            '<div style="display:flex;flex-wrap:wrap;gap:6px;font-size:12px;font-family:var(--font-mono);color:var(--text-muted);">';
        (batch.domains || []).forEach(function(dom) {
            html += '<span style="background:var(--bg-input);padding:2px 8px;border-radius:4px;">' + dom + '</span>';
        });
        html += '</div></div></div>';
    });

    document.getElementById('aging-pool-section').innerHTML = html;
}

async function activateAgingBatch(batchId, count) {
    if (!confirm('Activate ' + count + ' domains from this batch? They will be removed from the aging pool.')) return;
    try {
        var result = await apiPost('/api/aging-pool/activate', {batch_id: batchId, count: count});
        if (result.error) {
            showToast('Error: ' + result.error, 'error');
        } else {
            showToast('Activated ' + result.activated_domains.length + ' domains. ' + result.remaining + ' remaining.', 'success');
            loadAgingPool();
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

function showAddBatchModal() {
    var overlay = document.getElementById('add-batch-overlay');
    var modal = document.getElementById('add-batch-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'add-batch-overlay';
        overlay.className = 'modal-overlay';
        overlay.onclick = closeAddBatchModal;
        document.body.appendChild(overlay);

        modal = document.createElement('div');
        modal.id = 'add-batch-modal';
        modal.className = 'modal-panel';
        modal.innerHTML =
            '<h2>Add Aging Batch</h2>' +
            '<div style="margin-bottom:16px;"><label>Batch Name</label><input id="ab-name" type="text" placeholder="e.g. Service Industry .info"></div>' +
            '<div style="margin-bottom:16px;"><label>Purchase Date</label><input id="ab-date" type="date" value="' + new Date().toISOString().split('T')[0] + '"></div>' +
            '<div style="margin-bottom:16px;"><label>Cost ($)</label><input id="ab-cost" type="number" step="0.01" min="0" placeholder="0.00"></div>' +
            '<div style="margin-bottom:16px;"><label>NS Provider</label><input id="ab-ns" type="text" value="CloudNS"></div>' +
            '<div style="margin-bottom:16px;"><label>Domains (one per line)</label><textarea id="ab-domains" rows="8" style="width:100%;font-family:var(--font-mono);font-size:12px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius);padding:8px;" placeholder="domain1.info&#10;domain2.info"></textarea></div>' +
            '<div class="btn-row"><button class="btn btn-cancel" onclick="closeAddBatchModal()">Cancel</button><button class="btn btn-primary" onclick="submitAddBatch()">Add Batch</button></div>' +
            '<div id="ab-status" style="margin-top:12px;font-size:13px;"></div>';
        document.body.appendChild(modal);
    }
    overlay.style.display = 'block';
    modal.style.display = 'block';
}

function closeAddBatchModal() {
    var overlay = document.getElementById('add-batch-overlay');
    var modal = document.getElementById('add-batch-modal');
    if (overlay) overlay.style.display = 'none';
    if (modal) modal.style.display = 'none';
}

async function submitAddBatch() {
    var name = document.getElementById('ab-name').value.trim();
    var purchased = document.getElementById('ab-date').value;
    var cost = parseFloat(document.getElementById('ab-cost').value) || 0;
    var nsProvider = document.getElementById('ab-ns').value.trim();
    var domainsText = document.getElementById('ab-domains').value.trim();
    var domains = domainsText.split('\n').map(function(d) { return d.trim(); }).filter(function(d) { return d.length > 0; });

    if (!name || !purchased || domains.length === 0) {
        document.getElementById('ab-status').innerHTML = '<span style="color:var(--red);">Name, date, and at least one domain required.</span>';
        return;
    }

    try {
        var result = await apiPost('/api/aging-pool/add', {name: name, purchased: purchased, cost: cost, ns_provider: nsProvider, domains: domains});
        if (result.error) {
            document.getElementById('ab-status').innerHTML = '<span style="color:var(--red);">' + result.error + '</span>';
        } else {
            showToast('Added batch: ' + result.domain_count + ' domains', 'success');
            closeAddBatchModal();
            loadAgingPool();
        }
    } catch (err) {
        document.getElementById('ab-status').innerHTML = '<span style="color:var(--red);">Error: ' + err.message + '</span>';
    }
}
```

- [ ] **Step 3: Call `loadAgingPool()` from `loadDomains()`**

In `secondary-tabs.js`, modify the `loadDomains()` function (around line 109 after the new aging pool code is prepended). Add `loadAgingPool();` as the first line inside the `try` block, so the aging pool loads in parallel with domains:

Find:
```javascript
async function loadDomains() {
    document.getElementById('dom-content').innerHTML = '<div class="loading"><span class="spinner"></span> Loading domain registrar data...</div>';
    try {
        var resp = await fetch('/api/domains');
```

Replace with:
```javascript
async function loadDomains() {
    document.getElementById('dom-content').innerHTML = '<div class="loading"><span class="spinner"></span> Loading domain registrar data...</div>';
    loadAgingPool();
    try {
        var resp = await fetch('/api/domains');
```

- [ ] **Step 4: Test locally**

Run: `cd ~/email-infra && python3 dashboard.py 8888 &`

Open `http://localhost:8888` in browser, click Domains tab. Verify:
1. "Aging Pool" section appears at top with summary cards (70 domains, 5 B groups, Xd left)
2. Batch card shows "Service Industry .info" with progress bar
3. Clicking the card header expands domain list
4. "+ Add Batch" button opens the modal form

Kill the server: `kill %1`

- [ ] **Step 5: Commit**

```bash
git add dashboard.html js/secondary-tabs.js
git commit -m "feat: add aging pool UI section to Domains tab"
```

---

### Task 4: Sync dashboard to Vercel

**Files:**
- Copy: `dashboard.html` → `web/public/index.html`
- Copy: `js/secondary-tabs.js` → `web/public/js/secondary-tabs.js`

- [ ] **Step 1: Copy updated files**

```bash
cp ~/email-infra/dashboard.html ~/email-infra/web/public/index.html
cp ~/email-infra/js/secondary-tabs.js ~/email-infra/web/public/js/secondary-tabs.js
```

- [ ] **Step 2: Deploy to Vercel**

```bash
cd ~/email-infra/web && npx vercel --prod
```

- [ ] **Step 3: Commit sync**

```bash
git add web/public/index.html web/public/js/secondary-tabs.js
git commit -m "sync: deploy aging pool UI to Vercel"
```

---

### Task 5: Update memory

- [ ] **Step 1: Update the aging domains memory file**

Update `~/.claude/projects/-Users-aidanhutchinson/memory/project_generic_aging_domains.md` to reflect that the tracking is now in `state/aging_pool.json` with dashboard visibility, not just a static JSON file tracked by memory.
