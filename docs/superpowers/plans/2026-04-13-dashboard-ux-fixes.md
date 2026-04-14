# Dashboard UX Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three dashboard issues: glitchy loading, inflated client volumes for clients with warming replacements, and SR acquisition groups not appearing.

**Architecture:** All changes are in `dashboard.py` (backend API) and `dashboard.html` (frontend). No new files. Backend adds per-batch warmup info to client summaries. Frontend loads all sections before showing content and renders warming batches distinctly.

**Tech Stack:** Python (Flask), vanilla JS, HTML/CSS

---

### Task 1: Fix Glitchy Page Loading

**Files:**
- Modify: `dashboard.html:389-413` (loadOverview function)

The dashboard fires `loadOverview()`, then immediately cascades `loadUnassigned()`, `loadGenericGroups()`, and `loadAcquisition()`. Each section pops in independently causing layout shifts.

- [ ] **Step 1: Replace cascading loads with Promise.all**

In `dashboard.html`, replace the `loadOverview` function (lines 389-413):

```javascript
async function loadOverview() {
    document.getElementById('loading').style.display = 'block';
    document.getElementById('content').style.display = 'none';

    try {
        // Load all sections in parallel, show nothing until all complete
        const [overviewResp, unassignedResp, genericResp, acquisitionResp] = await Promise.all([
            fetch('/api/overview'),
            fetch('/api/unassigned').catch(() => ({ok: false})),
            fetch('/api/generic-groups').catch(() => ({ok: false})),
            fetch('/api/acquisition').catch(() => ({ok: false})),
        ]);

        const text = await overviewResp.text();
        try {
            overviewData = JSON.parse(text);
        } catch (parseErr) {
            document.getElementById('loading').innerHTML = 'Error parsing response: ' + text.substring(0, 500);
            return;
        }
        if (overviewData.error) {
            document.getElementById('loading').innerHTML = '<div style="text-align:left;max-width:800px;margin:0 auto;"><h3 style="color:#ff6b6b;">API Error</h3><pre style="white-space:pre-wrap;color:#ffaaaa;font-size:12px;">' + overviewData.traceback + '</pre></div>';
            return;
        }

        // Parse sub-section responses
        let unassignedData = null, genericData = null, acquisitionData = null;
        try { unassignedData = unassignedResp.ok !== false ? await unassignedResp.json() : null; } catch(e) {}
        try { genericData = genericResp.ok !== false ? await genericResp.json() : null; } catch(e) {}
        try { acquisitionData = acquisitionResp.ok !== false ? await acquisitionResp.json() : null; } catch(e) {}

        // Render everything at once
        renderOverview();
        if (unassignedData) renderUnassigned(unassignedData);
        if (genericData && genericData.groups && genericData.groups.length > 0) {
            document.getElementById('generic-section').style.display = 'block';
            document.getElementById('generic-stats').innerHTML = `
                <div class="stat-card"><div class="value">${genericData.total_accounts}</div><div class="label">Generic Inboxes</div></div>
                <div class="stat-card"><div class="value">${genericData.groups.length}</div><div class="label">Groups</div></div>
                <div class="stat-card"><div class="value">${genericData.total_daily_capacity}/day</div><div class="label">Total Capacity</div></div>
            `;
            renderGenericGroups(genericData.groups);
        } else {
            document.getElementById('generic-section').style.display = 'none';
        }
        if (acquisitionData && acquisitionData.total_groups > 0) {
            document.getElementById('acquisition-section').style.display = 'block';
            document.getElementById('acquisition-stats').innerHTML = `
                <div class="stat-card"><div class="value">${acquisitionData.total_accounts}</div><div class="label">Acquisition Inboxes</div></div>
                <div class="stat-card"><div class="value">${acquisitionData.total_groups}</div><div class="label">Active Groups</div></div>
            `;
            renderAcquisitionGroups(acquisitionData.groups);
        } else {
            document.getElementById('acquisition-section').style.display = 'none';
        }
    } catch (err) {
        document.getElementById('loading').innerHTML = 'Error loading data: ' + err.message;
    }
}
```

- [ ] **Step 2: Remove standalone loadUnassigned, loadGenericGroups, loadAcquisition calls**

Extract the rendering logic from `loadUnassigned()`, `loadGenericGroups()`, and `loadAcquisition()` into pure render functions that accept data (no fetch). The existing `renderGenericGroups()` and `renderAcquisitionGroups()` are already pure renderers. `loadUnassigned` needs its render extracted into `renderUnassigned(data)`.

Keep the original `loadGenericGroups()` and `loadAcquisition()` as-is for the 5-minute auto-refresh, but change `loadOverview`'s cascade to use the parallel approach above.

- [ ] **Step 3: Verify no layout shifts**

Run the dashboard, open in browser, confirm all sections appear simultaneously after the loading spinner.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "fix: load all dashboard sections in parallel before rendering"
```

---

### Task 2: Add Per-Batch Warmup Info to Client Cards

**Files:**
- Modify: `dashboard.py:490-609` (_compute_overview client loop)
- Modify: `dashboard.html:478-531` (client card template)

Clients like Shade Tree have multiple infrastructure batches (old production + new warming replacements). The dashboard currently shows one warmup date and total capacity for the whole client. Need to show batches separately.

- [ ] **Step 1: Add batch detection to backend _compute_overview**

In `dashboard.py`, after the per-account warmup classification loop (around line 519), group accounts by warmup start date to identify batches:

```python
        # Group accounts into batches by warmup start week
        # (accounts started within 3 days of each other = same batch)
        batch_dates = {}
        for a in cl_accounts:
            wd = a.get("warmup_details") or {}
            wc = wd.get("warmup_created_at", "")
            if not wc:
                continue
            date_str = wc[:10]
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                # Round to nearest 3-day bucket
                bucket = (d - datetime(2020, 1, 1)).days // 3
                batch_dates.setdefault(bucket, {"date": d, "total": 0, "ready": 0, "warming": 0})
                batch_dates[bucket]["total"] += 1
                days_since = (now_dt - d).days
                if days_since >= 14 or a.get("campaign_count", 0) > 0:
                    batch_dates[bucket]["ready"] += 1
                else:
                    batch_dates[bucket]["warming"] += 1
            except (ValueError, TypeError):
                pass

        batches = []
        for bucket in sorted(batch_dates.keys()):
            b = batch_dates[bucket]
            days_since = (now_dt - b["date"]).days
            batches.append({
                "warmup_start": b["date"].strftime("%Y-%m-%d"),
                "total": b["total"],
                "ready": b["ready"],
                "warming": b["warming"],
                "days_done": min(14, days_since),
                "status": "ready" if days_since >= 14 else "warming",
            })
```

Add `"batches": batches` to the client_summaries dict (around line 608).

- [ ] **Step 2: Update frontend client card to show batches**

In `dashboard.html`, replace the single warmup progress bar (line 521) with batch-aware rendering:

```javascript
// Replace the single warmup bar with batch info
${cl.batches && cl.batches.length > 1 ? cl.batches.map(b => {
    if (b.status === 'ready') {
        return `<div style="margin-top:6px;display:flex;justify-content:space-between;align-items:center;font-size:12px;">
            <span style="color:#4ecdc4;">&#9679; ${b.total} accounts ready</span>
            <span style="color:#888;">since ${b.warmup_start}</span>
        </div>`;
    } else {
        const pct = Math.round(b.days_done / 14 * 100);
        return `<div style="margin-top:6px;">
            <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:3px;">
                <span style="color:#7c4dff;">&#9679; ${b.total} new accounts warming</span>
                <span>Day ${b.days_done}/14</span>
            </div>
            <div style="background:#0a1628;border-radius:4px;height:5px;overflow:hidden;">
                <div style="background:#7c4dff;height:100%;width:${pct}%;border-radius:4px;"></div>
            </div>
        </div>`;
    }
}).join('') : (cl.warming > 0 && cl.warmup_days_done !== null ? `<div style="margin-top:10px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:4px;"><span>Warmup Progress</span><span>${cl.warmup_progress}</span></div><div style="background:#0a1628;border-radius:4px;height:6px;overflow:hidden;"><div style="background:#7c4dff;height:100%;width:${Math.round(cl.warmup_days_done / 14 * 100)}%;border-radius:4px;"></div></div></div>` : '')}
```

- [ ] **Step 3: Verify with Shade Tree and Kay's**

Run `python3 -c "import dashboard; ..."` to confirm:
- Shade Tree shows 2 batches (63 ready + 18 warming)
- Kay's shows 2 batches (33 ready + 51 warming or similar)
- Daily capacity reflects only production-ready accounts

- [ ] **Step 4: Commit**

```bash
git add dashboard.py dashboard.html
git commit -m "feat: show per-batch warmup progress on client cards"
```

---

### Task 3: Verify SR Acquisition Groups Render in Dashboard

**Files:**
- Modify: `dashboard.py` (if needed)
- Modify: `dashboard.html` (if needed)

The backend `api_acquisition()` already returns SR groups. The frontend `renderAcquisitionGroups()` renders whatever is in the groups array. This should already work. Need to verify end-to-end.

- [ ] **Step 1: Run dashboard server and hit /api/acquisition**

```bash
cd ~/email-infra && python3 -c "
import dashboard
import json
result = dashboard.api_acquisition()
for g in result['groups']:
    print(f'{g[\"name\"]}: {g[\"accounts\"]} accounts')
"
```

Confirm SR-A through SR-D appear.

- [ ] **Step 2: Test in browser**

Start the dashboard server, open in browser, scroll to Acquisition Groups section. Confirm SR-A through SR-D cards render with correct account counts.

- [ ] **Step 3: Fix any rendering issues found**

If SR groups render but look wrong (e.g., missing data), fix the `_compute_group_stats` function or frontend template.

- [ ] **Step 4: Commit if changes were needed**

```bash
git add dashboard.py dashboard.html
git commit -m "fix: ensure SR acquisition groups render in dashboard"
```

---

### Task 4: End-to-End Smoke Test

- [ ] **Step 1: Start dashboard, verify all sections load simultaneously**
- [ ] **Step 2: Verify Shade Tree card shows batched warmup info**
- [ ] **Step 3: Verify Kay's card shows batched warmup info**
- [ ] **Step 4: Verify SR-A through SR-D in acquisition section**
- [ ] **Step 5: Click into Shade Tree detail panel, verify account table loads**
- [ ] **Step 6: Final commit if any fixes needed**
