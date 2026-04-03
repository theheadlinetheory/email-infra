# Infrastructure Health Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add health scoring, alerting, and infrastructure rotation recommendations to the email-infra dashboard so Aidan can monitor all client infrastructure in ≤10 min/day.

**Architecture:** Backend calculates composite health scores per inbox using 7-day rolling averages, groups by domain (any bad inbox = whole domain flagged), and surfaces client-level health. Frontend displays health scores on cards and detail panel, with an alert banner for clients needing attention. Rotation pipeline (Phase 2, separate plan) will handle automated replacement.

**Tech Stack:** Python 3 (dashboard.py backend), vanilla HTML/CSS/JS (dashboard.html frontend), SmartLead API (public + internal)

---

## Phase 1: Health Scoring & Display

### Task 1: Backend — Health Score Calculation

**Files:**
- Modify: `dashboard.py:234-248` (change `get_health_metrics` from 14-day to 7-day default)
- Modify: `dashboard.py:266-390` (add health scoring to `api_overview`)
- Modify: `dashboard.py:393-421` (add health scoring to `api_client_accounts`)

- [ ] **Step 1: Change health metrics window to 7 days**

In `dashboard.py`, change line 234:

```python
def get_health_metrics(days=14):
```
to:
```python
def get_health_metrics(days=7):
```

- [ ] **Step 2: Add health score calculation function**

Add after `get_warmup_start_dates()` (after line 263), before `# --- API endpoint logic ---`:

```python
def calculate_health_score(account, health_data):
    """Calculate composite health score (0-100) for an inbox.

    Weights: bounce_rate 30%, reply_rate 25%, reputation 25%,
    inbox_placement 10%, smtp/imap 5%, blocked 5%.
    Returns dict with score and list of flag reasons.
    """
    email = account.get("from_email", "")
    h = health_data.get(email, {})
    wd = account.get("warmup_details", {})
    flags = []

    # Bounce rate (30%) — 0pts at >3%, 100pts at ≤1%, linear between
    br = float(h["bounce_rate"]) if h.get("bounce_rate") is not None else None
    if br is not None:
        if br > 3:
            bounce_score = 0
            flags.append("bounce")
        elif br <= 1:
            bounce_score = 100
        else:
            bounce_score = 100 - ((br - 1) / 2) * 100
    else:
        bounce_score = 50  # no data, neutral

    # Reply rate (25%) — 0pts at <2%, 100pts at ≥5%, linear between
    rr = float(h["reply_rate"]) if h.get("reply_rate") is not None else None
    if rr is not None:
        if rr < 2:
            reply_score = 0
            flags.append("reply")
        elif rr >= 5:
            reply_score = 100
        else:
            reply_score = ((rr - 2) / 3) * 100
    else:
        reply_score = 50

    # Reputation (25%) — 0pts at <99, 100pts at ≥99
    rep_raw = wd.get("warmup_reputation", "?")
    try:
        rep = float(rep_raw)
        if rep < 99:
            rep_score = 0
            flags.append("reputation")
        else:
            rep_score = 100
    except (ValueError, TypeError):
        rep_score = 50

    # Inbox placement (10%) — use warmup spam ratio as proxy
    sent = wd.get("total_sent_count", 0) or 0
    spam = wd.get("total_spam_count", 0) or 0
    if sent > 0:
        placement = ((sent - spam) / sent) * 100
        if placement < 99:
            placement_score = 0
            flags.append("placement")
        else:
            placement_score = 100
    else:
        placement_score = 50

    # SMTP/IMAP (5%) — binary
    smtp_ok = account.get("is_smtp_success", False)
    imap_ok = account.get("is_imap_success", False)
    if not smtp_ok or not imap_ok:
        conn_score = 0
        flags.append("smtp")
    else:
        conn_score = 100

    # Blocked (5%) — binary
    blocked = wd.get("status") not in ("ACTIVE", None) and wd.get("blocked_reason")
    if blocked:
        block_score = 0
        flags.append("blocked")
    else:
        block_score = 100

    # Warmup off flag (not scored, but flagged)
    warmup_status = wd.get("status")
    if warmup_status != "ACTIVE" and not blocked:
        flags.append("warmup_off")

    score = round(
        bounce_score * 0.30 +
        reply_score * 0.25 +
        rep_score * 0.25 +
        placement_score * 0.10 +
        conn_score * 0.05 +
        block_score * 0.05
    )

    return {"score": score, "flags": flags}


def group_accounts_by_domain(accounts_with_scores):
    """Group accounts by domain. If ANY account on a domain is flagged,
    mark ALL accounts on that domain as flagged (domain-level rollup)."""
    by_domain = {}
    for acc in accounts_with_scores:
        domain = acc["email"].split("@")[-1] if "@" in acc["email"] else ""
        if domain not in by_domain:
            by_domain[domain] = []
        by_domain[domain].append(acc)

    # If any inbox on the domain has flags, add "domain_flagged" to all
    for domain, accs in by_domain.items():
        domain_has_flags = any(a["health_flags"] for a in accs)
        if domain_has_flags:
            for a in accs:
                a["domain_flagged"] = True
        else:
            for a in accs:
                a["domain_flagged"] = False

    return by_domain
```

- [ ] **Step 3: Add health score to api_overview client summaries**

In `dashboard.py`, in `api_overview()`, after the avg_bounce/avg_reply calculation (around line 349-350), add health scoring per client. Replace the `client_summaries.append({...})` block (lines 352-371) with:

```python
        # Calculate health scores for each account
        cl_scores = []
        flagged_domains = set()
        for a in cl_accounts:
            hs = calculate_health_score(a, health)
            cl_scores.append(hs["score"])
            if hs["flags"]:
                domain = a.get("from_email", "").split("@")[-1]
                flagged_domains.add(domain)

        # Count total domains
        all_cl_domains = set(
            a.get("from_email", "").split("@")[-1] for a in cl_accounts
        )
        total_domains = len(all_cl_domains)
        flagged_pct = (len(flagged_domains) / total_domains * 100) if total_domains > 0 else 0
        avg_health = round(sum(cl_scores) / len(cl_scores)) if cl_scores else 0

        # Warmup progress: count fully warmed (14+ days since warmup start)
        warmed_count = 0
        warming_count = 0
        for a in cl_accounts:
            ws = a.get("warmup_details", {}).get("status")
            if ws == "ACTIVE":
                warming_count += 1
                # Consider "fully warmed" if reputation >= 99
                rep = a.get("warmup_details", {}).get("warmup_reputation", "?")
                try:
                    if float(rep) >= 99:
                        warmed_count += 1
                except (ValueError, TypeError):
                    pass

        client_summaries.append({
            "id": cl["id"],
            "name": cl["name"],
            "accounts": len(cl_accounts),
            "warming": cl_warming,
            "in_campaign": cl_campaigns,
            "smtp_failures": cl_smtp_fail,
            "blocked": cl_blocked,
            "warmup_start": ws_date,
            "ready_date": ready_date,
            "days_until_ready": days_left,
            "rotation_date": rotation_date,
            "days_until_rotation": rotation_days,
            "health_accounts": cl_health_count,
            "total_sent": cl_sent,
            "total_bounced": cl_bounced,
            "total_replied": cl_replied,
            "avg_bounce_rate": avg_bounce,
            "avg_reply_rate": avg_reply,
            "health_score": avg_health,
            "total_domains": total_domains,
            "flagged_domains": len(flagged_domains),
            "flagged_pct": round(flagged_pct, 1),
            "needs_attention": flagged_pct >= 15,
            "warmed_count": warmed_count,
            "warming_count": warming_count,
        })
```

- [ ] **Step 4: Add attention_count to overview response**

In `api_overview()`, before the `return` statement (around line 380), add:

```python
    attention_count = sum(1 for c in client_summaries if c.get("needs_attention"))
```

And add to the return dict:

```python
        "attention_count": attention_count,
```

- [ ] **Step 5: Add health score to api_client_accounts**

In `api_client_accounts()`, modify the account dict in the result list (around line 401-420) to include health data. After the existing fields, replace the function:

```python
def api_client_accounts(client_id):
    accounts = get_accounts_by_client(int(client_id))
    health = get_health_metrics()
    result = []
    for a in accounts:
        wd = a.get("warmup_details", {})
        email = a.get("from_email", "")
        h = health.get(email, {})
        hs = calculate_health_score(a, health)

        # Warmup progress: days since warmup enabled
        warmup_enabled = wd.get("warmup_enabled_date")
        warmup_days = None
        if warmup_enabled:
            try:
                from datetime import datetime
                enabled_dt = datetime.strptime(warmup_enabled[:10], "%Y-%m-%d")
                warmup_days = (datetime.now() - enabled_dt).days
            except Exception:
                pass

        result.append({
            "id": a["id"],
            "email": email,
            "domain": email.split("@")[-1],
            "warmup_status": wd.get("status", "UNKNOWN"),
            "warmup_sent": wd.get("total_sent_count", 0),
            "warmup_spam": wd.get("total_spam_count", 0),
            "warmup_reputation": wd.get("warmup_reputation", "?"),
            "blocked_reason": wd.get("blocked_reason"),
            "campaign_count": a.get("campaign_count", 0),
            "daily_sent": a.get("daily_sent_count", 0),
            "smtp_ok": a.get("is_smtp_success", False),
            "imap_ok": a.get("is_imap_success", False),
            "bounce_rate": h.get("bounce_rate"),
            "reply_rate": h.get("reply_rate"),
            "health_sent": h.get("total_sent", 0),
            "health_bounced": h.get("total_bounced", 0),
            "health_replied": h.get("total_replied", 0),
            "health_score": hs["score"],
            "health_flags": hs["flags"],
            "warmup_days": warmup_days,
        })

    # Domain-level rollup
    by_domain = group_accounts_by_domain(result)
    for domain, accs in by_domain.items():
        domain_has_flags = any(a["health_flags"] for a in accs)
        for a in accs:
            a["domain_flagged"] = domain_has_flags

    # Count flagged domains for replacement recommendation
    flagged_domains = [d for d, accs in by_domain.items() if any(a["health_flags"] for a in accs)]
    flagged_inbox_count = sum(len(accs) for d, accs in by_domain.items() if d in flagged_domains)
    replacement_domains_needed = len(flagged_domains)
    replacement_inboxes = replacement_domains_needed * 3  # 3 inboxes per domain

    return {
        "client_id": int(client_id),
        "accounts": result,
        "flagged_domains": flagged_domains,
        "flagged_inbox_count": flagged_inbox_count,
        "replacement_domains_needed": replacement_domains_needed,
        "replacement_inboxes": replacement_inboxes,
    }
```

- [ ] **Step 6: Remove open_rate from detail table response**

In the new `api_client_accounts`, note that `open_rate` is NOT included (already done in Step 5 code above). This aligns with the requirement to stop tracking open rate.

- [ ] **Step 7: Commit backend health scoring**

```bash
cd ~/email-infra
git add dashboard.py
git commit -m "feat: add health scoring system — composite scores, domain rollup, 7-day window"
```

### Task 2: Frontend — Health Score on Client Cards

**Files:**
- Modify: `dashboard.html:245-295` (renderOverview client cards)

- [ ] **Step 1: Update summary row thresholds**

In `dashboard.html`, update the bounce rate color thresholds in the summary row (around line 249-250). Change:

```javascript
    const obColor = overallBounce !== '—' ? (parseFloat(overallBounce) > 5 ? 'alert' : parseFloat(overallBounce) > 2 ? 'warn' : 'good') : 'good';
    const orColor = overallReply !== '—' ? (parseFloat(overallReply) > 10 ? 'good' : parseFloat(overallReply) > 5 ? 'warn' : 'alert') : 'good';
```

to:

```javascript
    const obColor = overallBounce !== '—' ? (parseFloat(overallBounce) > 3 ? 'alert' : parseFloat(overallBounce) > 1 ? 'warn' : 'good') : 'good';
    const orColor = overallReply !== '—' ? (parseFloat(overallReply) > 5 ? 'good' : parseFloat(overallReply) > 2 ? 'warn' : 'alert') : 'good';
```

- [ ] **Step 2: Add health score badge and warmup bar to client cards**

In `dashboard.html`, update the client card rendering (the template inside `gridEl.innerHTML = d.clients.map(cl => {...}).join('')`). Replace the entire client card template (lines ~275-294) with:

```javascript
        const healthColor = cl.health_score >= 85 ? '#4ecdc4' : cl.health_score >= 60 ? '#ffd93d' : '#ff6b6b';
        const healthBg = cl.health_score >= 85 ? '#1a4a3a' : cl.health_score >= 60 ? '#4a3a1a' : '#4a1a1a';

        return `
        <div class="client-card ${hasAlert || cl.needs_attention ? 'has-alert' : ''}" onclick="openDetail(${cl.id}, '${cl.name.replace(/'/g, "\\'")}')">
            <div class="cc-header">
                <span class="cc-name">${cl.name}</span>
                <div style="display:flex;align-items:center;gap:8px;">
                    <span class="badge" style="background:${healthBg};color:${healthColor};font-size:13px;padding:3px 10px;">${cl.health_score}</span>
                    <span class="cc-count">${cl.accounts} accounts</span>
                </div>
            </div>
            ${cl.needs_attention ? '<div style="background:#4a1a1a;border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:12px;color:#ff6b6b;">' + cl.flagged_domains + '/' + cl.total_domains + ' domains flagged (' + cl.flagged_pct + '%)</div>' : ''}
            <div class="cc-stats">
                <div class="cc-stat"><span class="label">Warming</span><span>${cl.warming}</span></div>
                <div class="cc-stat"><span class="label">In Campaign</span><span>${cl.in_campaign}</span></div>
                <div class="cc-stat"><span class="label">SMTP Fail</span><span style="color:${cl.smtp_failures > 0 ? '#ff6b6b' : '#4ecdc4'}">${cl.smtp_failures}</span></div>
                <div class="cc-stat"><span class="label">Blocked</span><span style="color:${cl.blocked > 0 ? '#ff6b6b' : '#4ecdc4'}">${cl.blocked}</span></div>
                <div class="cc-stat"><span class="label">Bounce Rate</span><span style="color:${cl.avg_bounce_rate !== null ? (cl.avg_bounce_rate > 3 ? '#ff6b6b' : cl.avg_bounce_rate > 1 ? '#ffd93d' : '#4ecdc4') : '#888'}">${cl.avg_bounce_rate !== null ? cl.avg_bounce_rate + '%' : '—'}</span></div>
                <div class="cc-stat"><span class="label">Reply Rate</span><span style="color:${cl.avg_reply_rate !== null ? (cl.avg_reply_rate > 5 ? '#4ecdc4' : cl.avg_reply_rate > 2 ? '#ffd93d' : '#ff6b6b') : '#888'}">${cl.avg_reply_rate !== null ? cl.avg_reply_rate + '%' : '—'}</span></div>
            </div>
            ${cl.warming_count > 0 ? '<div style="margin-top:10px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:4px;"><span>Warmup Progress</span><span>' + cl.warmed_count + '/' + cl.warming_count + ' fully warmed</span></div><div style="background:#0a1628;border-radius:4px;height:6px;overflow:hidden;"><div style="background:' + (cl.warmed_count === cl.warming_count ? '#4ecdc4' : '#7c4dff') + ';height:100%;width:' + (cl.warming_count > 0 ? Math.round(cl.warmed_count / cl.warming_count * 100) : 0) + '%;border-radius:4px;"></div></div></div>' : ''}
            <div class="cc-dates">
                ${cl.warmup_start ? '<div class="date-row"><span>Warmup Start</span><span>' + cl.warmup_start + '</span></div>' : ''}
                ${cl.ready_date ? '<div class="date-row"><span>Ready Date</span><span>' + cl.ready_date + ' ' + readyBadge + '</span></div>' : ''}
                ${cl.rotation_date ? '<div class="date-row"><span>Rotation Date</span><span>' + cl.rotation_date + ' ' + rotBadge + '</span></div>' : ''}
            </div>
        </div>`;
```

- [ ] **Step 3: Commit frontend client card changes**

```bash
cd ~/email-infra
git add dashboard.html
git commit -m "feat: add health score badges and warmup progress bars to client cards"
```

### Task 3: Frontend — Alert Banner for Health Issues

**Files:**
- Modify: `dashboard.html:220-240` (alert banner rendering in renderOverview)

- [ ] **Step 1: Add health attention alerts to the banner**

In `dashboard.html`, update the alert banner section in `renderOverview()`. Replace the condition and content (lines ~221-240):

```javascript
    const alertEl = document.getElementById('alert-banner');
    const attentionClients = d.clients.filter(c => c.needs_attention);
    if (d.blocked_accounts.length > 0 || d.smtp_failures > 0 || attentionClients.length > 0) {
        let html = '<div class="alert-banner"><h3>Alerts</h3>';
        if (attentionClients.length > 0) {
            html += '<div class="alert-item" style="font-size:14px;margin-bottom:6px;">' + attentionClients.length + ' client(s) have infrastructure that needs attention</div>';
            attentionClients.forEach(c => {
                html += '<div class="alert-item" style="padding-left:16px;">' + c.name + ' — ' + c.flagged_domains + '/' + c.total_domains + ' domains flagged (health score: ' + c.health_score + ')</div>';
            });
        }
        if (d.smtp_failures > 0) html += '<div class="alert-item">' + d.smtp_failures + ' accounts with SMTP failures</div>';
        if (d.imap_failures > 0) html += '<div class="alert-item">' + d.imap_failures + ' accounts with IMAP failures</div>';
        const grouped = {};
        d.blocked_accounts.forEach(b => {
            const short = b.reason.split(':')[0] || 'Unknown';
            if (!grouped[short]) grouped[short] = [];
            grouped[short].push(b.email.split('@')[1]);
        });
        for (const [reason, domains] of Object.entries(grouped)) {
            const unique = [...new Set(domains)];
            html += '<div class="alert-item">' + unique.length + ' domain(s) blocked — ' + reason + ': ' + unique.join(', ') + '</div>';
        }
        html += '</div>';
        alertEl.innerHTML = html;
    } else {
        alertEl.innerHTML = '';
    }
```

- [ ] **Step 2: Commit alert banner**

```bash
cd ~/email-infra
git add dashboard.html
git commit -m "feat: add health attention alerts to dashboard banner"
```

### Task 4: Frontend — Detail Panel Health Scores & Flags

**Files:**
- Modify: `dashboard.html:322-347` (renderDetailTable function)

- [ ] **Step 1: Update detail table to show health scores, flags, and warmup progress**

Replace the entire `renderDetailTable` function:

```javascript
function renderDetailTable(data) {
    const accounts = data.accounts;

    // Replacement recommendation
    let recHtml = '';
    if (data.flagged_domains && data.flagged_domains.length > 0) {
        recHtml = `<div style="background:#4a1a1a;border:1px solid #8b3a3a;border-radius:8px;padding:14px 18px;margin-bottom:16px;">
            <div style="font-size:14px;color:#ff6b6b;font-weight:600;margin-bottom:6px;">Infrastructure Replacement Needed</div>
            <div style="font-size:13px;color:#ffaaaa;">${data.flagged_inbox_count} inbox(es) across ${data.flagged_domains.length} domain(s) are unhealthy.</div>
            <div style="font-size:13px;color:#ffaaaa;margin-bottom:10px;">Recommended: Set up ${data.replacement_domains_needed} new domain(s) (${data.replacement_inboxes} inboxes).</div>
            <div style="font-size:12px;color:#888;">Flagged domains: ${data.flagged_domains.join(', ')}</div>
        </div>`;
    }

    let html = recHtml;
    html += '<table style="width:100%; border-collapse:collapse; font-size:13px;">';
    html += '<thead><tr><th>Email</th><th>Health</th><th>Warmup</th><th>Rep</th><th>Bounce</th><th>Reply</th><th>Sent</th><th>Campaigns</th><th>SMTP</th></tr></thead><tbody>';

    accounts.forEach(a => {
        const statusColor = a.warmup_status === 'ACTIVE' ? '#4ecdc4' : (a.blocked_reason ? '#ff6b6b' : '#ffd93d');
        const br = a.bounce_rate !== null ? parseFloat(a.bounce_rate) : null;
        const rr = a.reply_rate !== null ? parseFloat(a.reply_rate) : null;
        const brColor = br !== null ? (br > 3 ? '#ff6b6b' : br > 1 ? '#ffd93d' : '#4ecdc4') : '#888';
        const rrColor = rr !== null ? (rr > 5 ? '#4ecdc4' : rr > 2 ? '#ffd93d' : '#ff6b6b') : '#888';

        const healthColor = a.health_score >= 85 ? '#4ecdc4' : a.health_score >= 60 ? '#ffd93d' : '#ff6b6b';
        const healthBg = a.health_score >= 85 ? '#1a4a3a' : a.health_score >= 60 ? '#4a3a1a' : '#4a1a1a';
        const rowBg = a.domain_flagged ? 'background:#2a1a1a;' : '';

        // Flag icons
        const flagIcons = (a.health_flags || []).map(f => {
            const icons = {bounce:'B',reply:'R',reputation:'REP',placement:'P',smtp:'SMTP',blocked:'BLK',warmup_off:'WU'};
            return '<span style="background:#4a1a1a;color:#ff6b6b;padding:1px 4px;border-radius:3px;font-size:10px;margin-left:2px;" title="' + f + '">' + (icons[f]||f) + '</span>';
        }).join('');

        // Warmup progress bar
        let warmupCell = `<span style="color:${statusColor}">${a.warmup_status}</span>`;
        if (a.warmup_status === 'ACTIVE' && a.warmup_days !== null && a.warmup_days < 14) {
            const pct = Math.min(100, Math.round(a.warmup_days / 14 * 100));
            warmupCell += `<div style="margin-top:4px;background:#0a1628;border-radius:3px;height:4px;width:80px;"><div style="background:#7c4dff;height:100%;width:${pct}%;border-radius:3px;"></div></div><div style="font-size:10px;color:#888;margin-top:2px;">${a.warmup_days}d / 14d</div>`;
        }
        if (a.blocked_reason) {
            warmupCell += `<br><small style="color:#ff6b6b">${a.blocked_reason}</small>`;
        }

        html += `<tr style="${rowBg}">
            <td>${a.email}</td>
            <td><span style="background:${healthBg};color:${healthColor};padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;">${a.health_score}</span>${flagIcons}</td>
            <td>${warmupCell}</td>
            <td>${a.warmup_reputation}</td>
            <td style="color:${brColor}">${br !== null ? br.toFixed(1) + '%' : '—'}</td>
            <td style="color:${rrColor}">${rr !== null ? rr.toFixed(1) + '%' : '—'}</td>
            <td>${a.health_sent || a.warmup_sent}</td>
            <td>${a.campaign_count}</td>
            <td style="color:${a.smtp_ok ? '#4ecdc4' : '#ff6b6b'}">${a.smtp_ok ? 'OK' : 'FAIL'}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('detail-content').innerHTML = html;
}
```

Also update `openDetail` to pass the full data object instead of just accounts:

```javascript
async function openDetail(clientId, clientName) {
    document.getElementById('detail-overlay').style.display = 'block';
    document.getElementById('detail-panel').style.display = 'block';
    document.getElementById('detail-title').textContent = clientName;
    document.getElementById('detail-content').innerHTML = '<div class="loading"><span class="spinner"></span> Loading accounts...</div>';

    try {
        const resp = await fetch('/api/client/' + clientId + '/accounts');
        const data = await resp.json();
        renderDetailTable(data);
    } catch (err) {
        document.getElementById('detail-content').innerHTML = 'Error: ' + err.message;
    }
}
```

- [ ] **Step 2: Commit detail panel changes**

```bash
cd ~/email-infra
git add dashboard.html
git commit -m "feat: add health scores, flags, and warmup progress to detail panel"
```

### Task 5: Final — Push and verify

- [ ] **Step 1: Push to trigger Render deploy**

```bash
cd ~/email-infra
git push origin main
```

- [ ] **Step 2: Verify deploy**

Check that the dashboard loads and shows health scores on client cards and detail panel.
