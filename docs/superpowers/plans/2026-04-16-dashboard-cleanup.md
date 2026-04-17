# Dashboard Cleanup & Untagged Inbox Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declutter the SmartLead dashboard tab — make mode buttons filter content, unify card design, add acquisition warmup bars, detect/fix untagged inboxes.

**Architecture:** `switchMode()` becomes a real filter that shows/hides section divs. Card templates are unified into a shared `renderCard()` helper. `_compute_group_stats()` gains batch and capacity data. A new `fix_untagged.py` script remediates untagged accounts, and the dashboard surfaces an untagged alert.

**Tech Stack:** Python 3.9, vanilla HTML/CSS/JS, Supabase, SmartLead API (public + internal)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `dashboard.html` | Modify | Mode filtering, unified card template, untagged alert |
| `dashboard.py` | Modify | Acquisition batch/capacity data, scoped summary stats, untagged detection |
| `web/public/index.html` | Copy | Mirror of dashboard.html |
| `fix_untagged.py` | Create | One-time script to find and tag untagged accounts |

---

### Task 1: Make Mode Buttons Filter Sections

**Files:**
- Modify: `dashboard.html:1574-1579` (switchMode function)
- Modify: `dashboard.html:838-855` (summary stats rendering)

- [ ] **Step 1: Rewrite switchMode() to hide/show sections**

In `dashboard.html`, replace the `switchMode()` function (lines 1574-1579):

```javascript
    function switchMode(mode) {
        currentMode = mode;
        localStorage.setItem('dashboardMode', mode);
        document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.mode-btn[onclick="switchMode('${mode}')"]`).classList.add('active');

        // Fulfillment-only sections
        const fulfillmentSections = ['clients-grid', 'generic-section', 'rotation-section', 'unassigned-section', 'generic-setup-tracker'];
        // Acquisition-only sections
        const acquisitionSections = ['acquisition-section'];
        // Shared sections (always visible when data exists)
        // setup-pipeline-section is shared

        if (mode === 'fulfillment') {
            fulfillmentSections.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = ''; });
            acquisitionSections.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
        } else {
            fulfillmentSections.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
            acquisitionSections.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = ''; });
        }

        if (overviewData) renderOverview();
    }
```

- [ ] **Step 2: Restore saved mode on page load**

In `dashboard.html`, find the `currentMode` variable declaration (around line 607, near other global vars). After it, add:

```javascript
    currentMode = localStorage.getItem('dashboardMode') || 'fulfillment';
```

Also, at the end of the `loadOverview()` function (around line 704, just before removing the loading screen), add:

```javascript
        // Apply saved mode button state
        document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
        const savedBtn = document.querySelector(`.mode-btn[onclick="switchMode('${currentMode}')"]`);
        if (savedBtn) savedBtn.classList.add('active');
```

- [ ] **Step 3: Scope summary stats to active mode**

In `dashboard.html`, replace the summary stats rendering (lines 838-855) with mode-aware stats:

```javascript
        // Summary stats — scoped to current mode
        const summaryEl = document.getElementById('summary-row');
        if (currentMode === 'fulfillment') {
            const allBounce = d.clients.filter(c => c.avg_bounce_rate !== null).map(c => c.avg_bounce_rate);
            const allReply = d.clients.filter(c => c.avg_reply_rate !== null).map(c => c.avg_reply_rate);
            const overallBounce = allBounce.length ? (allBounce.reduce((a,b) => a+b, 0) / allBounce.length).toFixed(1) : '—';
            const overallReply = allReply.length ? (allReply.reduce((a,b) => a+b, 0) / allReply.length).toFixed(1) : '—';
            const obColor = overallBounce !== '—' ? (parseFloat(overallBounce) > 3 ? 'alert' : parseFloat(overallBounce) > 1 ? 'warn' : 'good') : 'good';
            const orColor = overallReply !== '—' ? (parseFloat(overallReply) > 5 ? 'good' : parseFloat(overallReply) > 2 ? 'warn' : 'alert') : 'good';
            summaryEl.innerHTML = `
                <div class="stat-card good"><div class="value">${d.total_accounts}</div><div class="label">Total Accounts</div></div>
                <div class="stat-card good"><div class="value">${d.in_campaign}</div><div class="label">In Campaigns</div></div>
                <div class="stat-card ${obColor}"><div class="value">${overallBounce}${overallBounce !== '—' ? '%' : ''}</div><div class="label">Avg Bounce Rate</div></div>
                <div class="stat-card ${orColor}"><div class="value">${overallReply}${overallReply !== '—' ? '%' : ''}</div><div class="label">Avg Reply Rate</div></div>
            `;
        } else {
            // Acquisition mode — use acquisitionData
            const aq = window._acquisitionData;
            if (aq) {
                const aqBounce = aq.groups.filter(g => g.avg_bounce_rate > 0).map(g => g.avg_bounce_rate);
                const aqReply = aq.groups.filter(g => g.avg_reply_rate > 0).map(g => g.avg_reply_rate);
                const aqOverallBounce = aqBounce.length ? (aqBounce.reduce((a,b) => a+b, 0) / aqBounce.length).toFixed(1) : '—';
                const aqOverallReply = aqReply.length ? (aqReply.reduce((a,b) => a+b, 0) / aqReply.length).toFixed(1) : '—';
                const aqBColor = aqOverallBounce !== '—' ? (parseFloat(aqOverallBounce) > 3 ? 'alert' : parseFloat(aqOverallBounce) > 1 ? 'warn' : 'good') : 'good';
                const aqRColor = aqOverallReply !== '—' ? (parseFloat(aqOverallReply) > 5 ? 'good' : parseFloat(aqOverallReply) > 2 ? 'warn' : 'alert') : 'good';
                summaryEl.innerHTML = `
                    <div class="stat-card good"><div class="value">${aq.total_accounts}</div><div class="label">Total Accounts</div></div>
                    <div class="stat-card good"><div class="value">${aq.total_groups}</div><div class="label">Active Groups</div></div>
                    <div class="stat-card ${aqBColor}"><div class="value">${aqOverallBounce}${aqOverallBounce !== '—' ? '%' : ''}</div><div class="label">Avg Bounce Rate</div></div>
                    <div class="stat-card ${aqRColor}"><div class="value">${aqOverallReply}${aqOverallReply !== '—' ? '%' : ''}</div><div class="label">Avg Reply Rate</div></div>
                `;
            }
        }
```

- [ ] **Step 4: Store acquisitionData globally for summary stats**

In `dashboard.html`, in the `loadOverview()` function where acquisitionData is parsed (around line 650), add after parsing:

```javascript
        window._acquisitionData = acquisitionData;
```

- [ ] **Step 5: Apply mode filter after rendering**

At the end of `renderOverview()` (the function that calls all the sub-renders), add:

```javascript
        switchMode(currentMode);
```

This ensures sections are hidden/shown after all data renders.

- [ ] **Step 6: Verify mode switching works**

Start the dashboard, click Fulfillment — should see client cards, generic groups, rotation. Click Acquisition — should see only acquisition groups. Refresh — should remember the last mode.

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: make Fulfillment/Acquisition mode buttons filter dashboard sections"
```

---

### Task 2: Add Batch + Capacity Data to Acquisition Groups Backend

**Files:**
- Modify: `dashboard.py:1099-1166` (_compute_group_stats function)

- [ ] **Step 1: Add batch warmup computation and daily_capacity to _compute_group_stats**

In `dashboard.py`, replace `_compute_group_stats()` (lines 1099-1166) with this version that adds batch data, daily_capacity, and blocked count:

```python
def _compute_group_stats(group_name, group_id, accounts, health):
    """Compute health/performance stats for a list of accounts."""
    cl_scores = []
    warming = 0
    in_campaign = 0
    smtp_fail = 0
    blocked = 0
    cl_sent = 0
    cl_bounced = 0
    cl_replied = 0
    cl_bounce_rates = []
    cl_reply_rates = []
    flagged_domains = set()
    all_domains = set()

    now_dt = datetime.now()

    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        all_domains.add(domain)

        hs = calculate_health_score(acc, health)
        cl_scores.append(hs["score"])
        if hs["flags"]:
            flagged_domains.add(domain)

        h = health.get(email, {})
        sent = h.get("sent", 0) or 0
        bounced = h.get("bounced", 0) or 0
        replied = h.get("replied", 0) or 0
        cl_sent += sent
        cl_bounced += bounced
        cl_replied += replied
        br_val = parse_rate(h.get("bounce_rate"))
        if br_val is not None:
            cl_bounce_rates.append(br_val)
        rr_val = parse_rate(h.get("reply_rate"))
        if rr_val is not None:
            cl_reply_rates.append(rr_val)

        wd_status = (acc.get("warmup_details") or {}).get("status")
        if wd_status == "ACTIVE":
            warming += 1
        elif wd_status not in ("ACTIVE", None):
            blocked += 1
        if (acc.get("campaign_count", 0) or 0) > 0:
            in_campaign += 1
        if not acc.get("is_smtp_success"):
            smtp_fail += 1

    avg_health = round(sum(cl_scores) / len(cl_scores)) if cl_scores else 100
    avg_bounce = round(sum(cl_bounce_rates) / len(cl_bounce_rates), 2) if cl_bounce_rates else 0
    avg_reply = round(sum(cl_reply_rates) / len(cl_reply_rates), 2) if cl_reply_rates else 0
    total_domains = len(all_domains)

    # Batch warmup computation (same logic as _compute_overview)
    _batch_buckets = {}
    for a in accounts:
        wd = a.get("warmup_details") or {}
        wc = wd.get("warmup_created_at", "")
        if not wc:
            continue
        try:
            d = datetime.strptime(wc[:10], "%Y-%m-%d")
            bucket = (d - datetime(2020, 1, 1)).days // 3
            if bucket not in _batch_buckets:
                _batch_buckets[bucket] = {"date": d, "total": 0, "ready": 0, "warming": 0}
            _batch_buckets[bucket]["total"] += 1
            if (now_dt - d).days >= 14 or a.get("campaign_count", 0) > 0:
                _batch_buckets[bucket]["ready"] += 1
            else:
                _batch_buckets[bucket]["warming"] += 1
        except (ValueError, TypeError):
            pass

    batches = []
    for bucket in sorted(_batch_buckets.keys()):
        b = _batch_buckets[bucket]
        days_since = (now_dt - b["date"]).days
        batches.append({
            "warmup_start": b["date"].strftime("%Y-%m-%d"),
            "total": b["total"],
            "ready": b["ready"],
            "warming": b["warming"],
            "days_done": min(14, days_since),
            "status": "ready" if days_since >= 14 else "warming",
        })

    # Capacity: only production-ready (warmup done or in campaign), minus failures
    production = sum(1 for a in accounts
                     if (a.get("campaign_count", 0) or 0) > 0
                     or ((a.get("warmup_details") or {}).get("warmup_created_at", "") and
                         _warmup_days(a, now_dt) >= 14))
    healthy = max(0, production - min(smtp_fail, production) - min(blocked, production))
    daily_capacity = healthy * 15

    return {
        "id": group_id,
        "name": group_name,
        "accounts": len(accounts),
        "warming": warming,
        "in_campaign": in_campaign,
        "smtp_failures": smtp_fail,
        "blocked": blocked,
        "daily_capacity": daily_capacity,
        "total_sent": cl_sent,
        "total_bounced": cl_bounced,
        "total_replied": cl_replied,
        "avg_bounce_rate": avg_bounce,
        "avg_reply_rate": avg_reply,
        "health_score": avg_health,
        "total_domains": total_domains,
        "flagged_domains": len(flagged_domains),
        "flagged_pct": round(len(flagged_domains) / total_domains * 100) if total_domains else 0,
        "needs_attention": len(flagged_domains) / total_domains >= 0.15 if total_domains else False,
        "batches": batches,
    }
```

- [ ] **Step 2: Add _warmup_days helper**

In `dashboard.py`, just before `_compute_group_stats()` (before line 1099), add:

```python
def _warmup_days(account, now_dt):
    """Return number of days since warmup started for an account."""
    wc = (account.get("warmup_details") or {}).get("warmup_created_at", "")
    if not wc:
        return 999
    try:
        d = datetime.strptime(wc[:10], "%Y-%m-%d")
        return (now_dt - d).days
    except (ValueError, TypeError):
        return 999
```

- [ ] **Step 3: Verify acquisition API returns batch data**

```bash
cd /Users/aidanhutchinson/email-infra
curl -s http://127.0.0.1:8099/api/acquisition | python3 -c "
import sys, json
data = json.load(sys.stdin)
for g in data.get('groups', []):
    batches = g.get('batches', [])
    print(f'{g[\"name\"]}: {g[\"accounts\"]} accounts, capacity={g.get(\"daily_capacity\",\"?\")}/day, {len(batches)} batch(es)')
    for b in batches:
        print(f'  {b[\"warmup_start\"]}: {b[\"total\"]} total, {b[\"warming\"]} warming, day {b[\"days_done\"]}/14')
"
```

Expected: Each group shows batch data and daily_capacity.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: add batch warmup data and capacity to acquisition group stats"
```

---

### Task 3: Unified Card Template in Frontend

**Files:**
- Modify: `dashboard.html:712-776` (renderClientCards)
- Modify: `dashboard.html:1322-1347` (renderAcquisitionGroups)

- [ ] **Step 1: Create shared renderCardHTML helper function**

In `dashboard.html`, add this function before `renderClientCards()` (before line 712):

```javascript
    function renderCardHTML(item, options) {
        // item: {name, accounts, daily_capacity, smtp_failures, blocked, avg_bounce_rate, avg_reply_rate, batches, ready_date, days_until_ready, rotation_date, days_until_rotation, needs_attention, flagged_domains, total_domains, flagged_pct, id}
        // options: {onclick}
        const issues = (item.smtp_failures || 0) + (item.blocked || 0);
        const issuesColor = issues > 0 ? '#ef4444' : '#22c55e';
        const bounceVal = item.avg_bounce_rate !== null && item.avg_bounce_rate !== undefined ? item.avg_bounce_rate + '%' : '—';
        const bounceColor = item.avg_bounce_rate === null || item.avg_bounce_rate === undefined ? '#888' : item.avg_bounce_rate > 3 ? '#ef4444' : item.avg_bounce_rate > 1 ? '#f59e0b' : '#22c55e';
        const replyVal = item.avg_reply_rate !== null && item.avg_reply_rate !== undefined ? item.avg_reply_rate + '%' : '—';
        const replyColor = item.avg_reply_rate === null || item.avg_reply_rate === undefined ? '#888' : item.avg_reply_rate > 5 ? '#22c55e' : item.avg_reply_rate > 2 ? '#f59e0b' : '#ef4444';

        let html = `<div class="client-card ${item.needs_attention ? 'has-alert' : ''}" ${options.onclick || ''}>`;
        // Header
        html += `<div class="cc-header"><span class="cc-name">${item.name}</span><span class="cc-count">${item.accounts} accounts</span></div>`;
        // Alert banner
        if (item.needs_attention) {
            html += `<div style="background:var(--red-bg);border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:12px;color:var(--red);">${item.flagged_domains}/${item.total_domains} domains flagged (${item.flagged_pct}%)</div>`;
        }
        // Stats: Capacity, Issues, Bounce, Reply
        html += `<div class="cc-stats">`;
        html += `<div class="cc-stat"><span class="label">Capacity</span><span>${item.daily_capacity || 0}/day</span></div>`;
        html += `<div class="cc-stat"><span class="label">Issues</span><span style="color:${issuesColor}">${issues}</span></div>`;
        html += `<div class="cc-stat"><span class="label">Bounce Rate</span><span style="color:${bounceColor}">${bounceVal}</span></div>`;
        html += `<div class="cc-stat"><span class="label">Reply Rate</span><span style="color:${replyColor}">${replyVal}</span></div>`;
        html += `</div>`;
        // Batch warmup bars
        if (item.batches && item.batches.length > 0) {
            const warmingBatches = item.batches.filter(b => b.status === 'warming');
            const readyBatches = item.batches.filter(b => b.status === 'ready');
            if (warmingBatches.length > 0 || readyBatches.length > 1) {
                for (const b of item.batches) {
                    if (b.status === 'ready') {
                        html += `<div style="margin-top:6px;display:flex;justify-content:space-between;align-items:center;font-size:12px;"><span style="color:#4ecdc4;">&#9679; ${b.total} accounts ready</span><span style="color:#888;">since ${b.warmup_start}</span></div>`;
                    } else {
                        const pct = Math.round(b.days_done / 14 * 100);
                        html += `<div style="margin-top:6px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:3px;"><span style="color:#7c4dff;">&#9679; ${b.total} new accounts warming</span><span>Day ${b.days_done}/14</span></div><div style="background:#0a1628;border-radius:4px;height:5px;overflow:hidden;"><div style="background:#7c4dff;height:100%;width:${pct}%;border-radius:4px;"></div></div></div>`;
                    }
                }
            }
        }
        // Footer dates
        const hasReady = item.ready_date && item.days_until_ready !== null && item.days_until_ready > 0;
        const hasRotation = item.rotation_date;
        if (hasReady || hasRotation) {
            html += `<div style="margin-top:8px;display:flex;justify-content:space-between;font-size:12px;color:#888;">`;
            if (hasReady) {
                html += `<span>Ready: ${item.ready_date}</span>`;
            } else {
                html += `<span></span>`;
            }
            if (hasRotation) {
                const rotBadge = item.days_until_rotation !== null && item.days_until_rotation <= 7 ? ' <span style="color:#f59e0b;font-weight:600;">Rotate soon</span>' : '';
                html += `<span>Rotation: ${item.rotation_date}${rotBadge}</span>`;
            }
            html += `</div>`;
        }
        html += `</div>`;
        return html;
    }
```

- [ ] **Step 2: Rewrite renderClientCards to use shared helper**

In `dashboard.html`, replace the card template inside `renderClientCards()` (lines 729-774). The function iterates `clients` and builds HTML. Replace the card generation loop body to use `renderCardHTML`:

Find the line where the card HTML template starts (around line 729, the `.map(cl =>` inside renderClientCards). Replace the entire template literal for each card with:

```javascript
    return renderCardHTML(cl, {onclick: `onclick="openDetail(${cl.id}, '${cl.name.replace(/'/g, "\\'")}')"` });
```

Keep the archived/paused card rendering as-is (those have different layouts).

- [ ] **Step 3: Rewrite renderAcquisitionGroups to use shared helper**

In `dashboard.html`, replace the `renderAcquisitionGroups()` function (lines 1322-1347):

```javascript
    function renderAcquisitionGroups(groups) {
        const grid = document.getElementById('acquisition-grid');
        grid.innerHTML = groups.map(g => {
            return renderCardHTML(g, {onclick: `onclick="openDetail(${g.id}, '${g.name.replace(/'/g, "\\'")}')"` });
        }).join('');
    }
```

- [ ] **Step 4: Verify both card types render identically**

Restart dashboard, check both Fulfillment and Acquisition modes. Cards should show: name + count header, 4-stat row (Capacity, Issues, Bounce, Reply), batch warmup bars, footer dates.

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: unified card template for fulfillment and acquisition groups"
```

---

### Task 4: Create fix_untagged.py Script

**Files:**
- Create: `fix_untagged.py`

- [ ] **Step 1: Create the untagged account detection and remediation script**

Create `fix_untagged.py` in `/Users/aidanhutchinson/email-infra/`:

```python
#!/usr/bin/env python3
"""Find and fix SmartLead accounts missing required tags.

Every account must have 3 tags: Zapmail + ClientName + WarmupStartDate.
This script identifies accounts with missing tags and applies corrections.

Usage:
    python3 fix_untagged.py --dry-run    # report only, no changes
    python3 fix_untagged.py              # fix untagged accounts
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_GQL = "https://fe-gql.smartlead.ai/v1/graphql"
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")

ZAPMAIL_TAG_ID = 262254


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def get_all_accounts():
    """Fetch all SmartLead email accounts."""
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100",
            timeout=30,
        )
        batch = r.json() if r.status_code == 200 else []
        if not isinstance(batch, list) or not batch:
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    return accounts


def get_account_tags(account_id):
    """Fetch tags for an account via the internal details endpoint."""
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/{account_id}/details",
        headers=internal_headers(),
        timeout=15,
    )
    if r.status_code != 200:
        return []
    data = r.json().get("email_accounts_by_pk", {})
    mappings = data.get("email_account_tag_mappings", [])
    return [{"id": m["tag"]["id"], "name": m["tag"]["name"]} for m in mappings]


def get_all_tags():
    """Get all existing tags from SmartLead via GraphQL."""
    body = {"query": "{ tags { id name color } }"}
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    tags = r.json().get("data", {}).get("tags", [])
    return {t["name"]: t for t in tags}


def get_clients():
    """Fetch all SmartLead clients."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    return r.json() if r.status_code == 200 else []


def create_tag(name, color="#808080"):
    """Create a new tag via GraphQL."""
    body = {
        "query": "mutation($name: String!, $color: String!) { insert_tags_one(object: {name: $name, color: $color}) { id name } }",
        "variables": {"name": name, "color": color},
    }
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    return r.json().get("data", {}).get("insert_tags_one", {})


def tag_account(account_id, tag_ids, client_id=None):
    """Apply tags to an email account."""
    body = {"id": account_id, "tags": tag_ids, "clientId": client_id}
    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
        headers=internal_headers(),
        json=body,
        timeout=30,
    )
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="Find and fix untagged SmartLead accounts")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't apply tags")
    args = parser.parse_args()

    print("Fetching all accounts...")
    accounts = get_all_accounts()
    print(f"Found {len(accounts)} total accounts")

    print("Fetching clients...")
    clients = get_clients()
    client_map = {c["id"]: c["name"] for c in clients}

    print("Fetching existing tags...")
    all_tags = get_all_tags()
    tag_name_to_id = {name: t["id"] for name, t in all_tags.items()}

    # Scan each account for missing tags
    untagged = []
    partial = []
    correct = 0
    errors = 0

    print(f"\nScanning {len(accounts)} accounts for missing tags...")
    for i, acc in enumerate(accounts):
        if (i + 1) % 50 == 0:
            print(f"  Scanned {i + 1}/{len(accounts)}...")

        acc_id = acc["id"]
        try:
            tags = get_account_tags(acc_id)
        except Exception as e:
            print(f"  ERROR fetching tags for account {acc_id}: {e}")
            errors += 1
            time.sleep(0.5)
            continue
        time.sleep(0.3)

        tag_names = [t["name"] for t in tags]
        has_zapmail = any(t["id"] == ZAPMAIL_TAG_ID for t in tags)
        has_client = any(t["name"] in client_map.values() or "group" in t["name"].lower() for t in tags)
        # Date tags look like "M/D/YY" or "MM/DD/YY"
        has_date = any("/" in t["name"] and len(t["name"]) <= 8 for t in tags)

        if has_zapmail and has_client and has_date:
            correct += 1
            continue

        client_name = client_map.get(acc.get("client_id"), "Unknown")
        warmup_date = ""
        wd = acc.get("warmup_details") or {}
        wc = wd.get("warmup_created_at", "")
        if wc:
            try:
                d = datetime.strptime(wc[:10], "%Y-%m-%d")
                warmup_date = f"{d.month}/{d.day}/{str(d.year)[2:]}"
            except (ValueError, TypeError):
                pass

        entry = {
            "id": acc_id,
            "email": acc.get("from_email", ""),
            "client_id": acc.get("client_id"),
            "client_name": client_name,
            "current_tags": tag_names,
            "missing_zapmail": not has_zapmail,
            "missing_client": not has_client,
            "missing_date": not has_date,
            "warmup_date": warmup_date,
        }

        if len(tags) == 0:
            untagged.append(entry)
        else:
            partial.append(entry)

    print(f"\n{'='*60}")
    print(f"RESULTS:")
    print(f"  Correctly tagged: {correct}")
    print(f"  Completely untagged: {len(untagged)}")
    print(f"  Partially tagged: {len(partial)}")
    print(f"  Scan errors: {errors}")
    print(f"{'='*60}")

    if untagged:
        print(f"\nCompletely untagged accounts ({len(untagged)}):")
        for a in untagged:
            print(f"  {a['email']} — client: {a['client_name']}, warmup: {a['warmup_date'] or 'unknown'}")

    if partial:
        print(f"\nPartially tagged accounts ({len(partial)}):")
        for a in partial:
            missing = []
            if a["missing_zapmail"]:
                missing.append("Zapmail")
            if a["missing_client"]:
                missing.append("ClientName")
            if a["missing_date"]:
                missing.append("Date")
            print(f"  {a['email']} — missing: {', '.join(missing)} — has: {a['current_tags']}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # Fix accounts
    to_fix = untagged + partial
    if not to_fix:
        print("\nNo accounts to fix!")
        return

    print(f"\nFixing {len(to_fix)} accounts...")
    fixed = 0
    skipped = 0

    for entry in to_fix:
        needed_tag_ids = []

        # Zapmail tag
        if entry["missing_zapmail"]:
            needed_tag_ids.append(ZAPMAIL_TAG_ID)

        # Client name tag
        if entry["missing_client"]:
            client_tag_name = entry["client_name"]
            if client_tag_name not in tag_name_to_id:
                print(f"  Creating tag: {client_tag_name}")
                new_tag = create_tag(client_tag_name)
                if new_tag.get("id"):
                    tag_name_to_id[client_tag_name] = new_tag["id"]
                    time.sleep(0.3)
                else:
                    print(f"  SKIP {entry['email']}: could not create client tag")
                    skipped += 1
                    continue
            needed_tag_ids.append(tag_name_to_id[client_tag_name])

        # Date tag
        if entry["missing_date"] and entry["warmup_date"]:
            date_tag_name = entry["warmup_date"]
            if date_tag_name not in tag_name_to_id:
                print(f"  Creating tag: {date_tag_name}")
                new_tag = create_tag(date_tag_name, "#4a90d9")
                if new_tag.get("id"):
                    tag_name_to_id[date_tag_name] = new_tag["id"]
                    time.sleep(0.3)
                else:
                    print(f"  SKIP {entry['email']}: could not create date tag")
                    skipped += 1
                    continue
            needed_tag_ids.append(tag_name_to_id[date_tag_name])
        elif entry["missing_date"]:
            print(f"  SKIP date tag for {entry['email']}: no warmup_created_at")

        if not needed_tag_ids:
            continue

        # Get existing tag IDs to preserve them
        existing_tags = get_account_tags(entry["id"])
        existing_ids = [t["id"] for t in existing_tags]
        all_tag_ids = list(set(existing_ids + needed_tag_ids))

        try:
            tag_account(entry["id"], all_tag_ids, entry["client_id"])
            fixed += 1
            print(f"  Fixed: {entry['email']} (+{len(needed_tag_ids)} tags)")
        except Exception as e:
            print(f"  ERROR fixing {entry['email']}: {e}")
            skipped += 1
        time.sleep(0.5)

    print(f"\nDone. Fixed: {fixed}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run in dry-run mode to identify untagged accounts**

```bash
cd /Users/aidanhutchinson/email-infra
python3 fix_untagged.py --dry-run
```

Expected: A report showing which accounts are untagged or partially tagged.

- [ ] **Step 3: Run for real to fix untagged accounts**

After reviewing the dry-run output:

```bash
python3 fix_untagged.py
```

Expected: Each untagged account gets its 3 required tags applied.

- [ ] **Step 4: Commit**

```bash
git add fix_untagged.py
git commit -m "feat: add script to detect and fix untagged SmartLead accounts"
```

---

### Task 5: Add Untagged Alert Banner to Dashboard

**Files:**
- Modify: `dashboard.py` (add untagged count to overview and acquisition responses)
- Modify: `dashboard.html` (render alert banner)

- [ ] **Step 1: Add untagged detection to background sync**

The background sync already fetches account details. However, tag checking requires the internal details API per account, which is expensive. Instead, add a lightweight endpoint that checks tag status on-demand.

In `dashboard.py`, add this function before `api_acquisition()` (before line 1169):

```python
def api_untagged_count():
    """Quick check: count accounts with no tags via synced Supabase data.

    Falls back to checking client_id assignment as a proxy — accounts
    with no client_id are almost certainly untagged.
    """
    all_accounts = get_all_accounts()
    # Accounts with no client_id are untagged (no client = no client tag)
    no_client = [a for a in all_accounts if not a.get("client_id")]
    return {
        "untagged_count": len(no_client),
        "accounts": [{"id": a["id"], "email": a.get("from_email", "")} for a in no_client[:20]],
    }
```

- [ ] **Step 2: Wire the GET endpoint**

In `dashboard.py`, inside `do_GET()`, find the route chain (around line 2935). Add before the debug endpoint:

```python
                elif path == "/api/untagged-count":
                    self._json_response(api_untagged_count())
```

- [ ] **Step 3: Add untagged alert to frontend**

In `dashboard.html`, add an alert div in the SmartLead tab, just after the summary-row div (after line 349):

```html
            <div id="untagged-alert" style="display:none;background:#2d1b0e;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
                <span style="color:#f59e0b;font-size:13px;" id="untagged-alert-text"></span>
            </div>
```

- [ ] **Step 4: Fetch and render untagged alert in loadOverview**

In `dashboard.html`, add `fetch('/api/untagged-count').catch(() => null)` to the `Promise.all` array in `loadOverview()` (line 619). Destructure as `untaggedResp`.

Then after parsing responses (around line 655), add:

```javascript
        let untaggedData = null;
        try { untaggedData = untaggedResp && untaggedResp.ok ? await untaggedResp.json() : null; } catch(e) {}

        // Render untagged alert
        const untaggedAlert = document.getElementById('untagged-alert');
        if (untaggedData && untaggedData.untagged_count > 0) {
            document.getElementById('untagged-alert-text').textContent =
                '\u26a0 ' + untaggedData.untagged_count + ' accounts have no client assignment and may be missing tags. Run fix_untagged.py to remediate.';
            untaggedAlert.style.display = 'flex';
        } else {
            untaggedAlert.style.display = 'none';
        }
```

- [ ] **Step 5: Verify alert renders**

Restart dashboard. If untagged accounts exist, the alert banner should appear. If all accounts are tagged (after running fix_untagged.py), it should be hidden.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py dashboard.html
git commit -m "feat: add untagged account alert banner to dashboard"
```

---

### Task 6: Sync Dashboard to Vercel + Final Verification

**Files:**
- Copy: `dashboard.html` → `web/public/index.html`

- [ ] **Step 1: Copy and commit**

```bash
cp /Users/aidanhutchinson/email-infra/dashboard.html /Users/aidanhutchinson/email-infra/web/public/index.html
git add web/public/index.html
git commit -m "chore: sync dashboard.html to Vercel public dir"
```

- [ ] **Step 2: Deploy**

```bash
cd /Users/aidanhutchinson/email-infra/web && npx vercel --prod
```

- [ ] **Step 3: Final verification checklist**

1. Click Fulfillment — only fulfillment sections visible, summary stats scoped
2. Click Acquisition — only acquisition sections visible, summary stats scoped
3. Refresh page — mode persists
4. Fulfillment cards show: account count, capacity, issues, bounce, reply, batch bars, dates
5. Acquisition cards show: same layout as fulfillment cards
6. Acquisition groups with warming accounts show progress bars
7. Untagged alert shows/hides correctly
