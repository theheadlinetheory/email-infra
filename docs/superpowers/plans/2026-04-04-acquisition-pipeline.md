# Acquisition Pipeline Mode + Dashboard Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `--acquisition` mode to setup.py that provisions THT's own sending infrastructure (Aidan Hutchinson identity, THT domains, group-based tagging), and integrate acquisition groups into the dashboard alongside client infrastructure.

**Architecture:** The pipeline gains a `mode` field ("client" or "acquisition") stored in the config. In acquisition mode, it swaps the sender identity, includes headlinetheory domains instead of excluding them, tags with group letters, and uses theheadlinetheory.com as forwarding. The dashboard gains a dedicated "Acquisition Groups" section that shows groups A-F (and future groups) with the same health/rotation metrics as clients.

**Tech Stack:** Python, SmartLead API, Zapmail API, Google Sheets API, vanilla HTML/JS dashboard

---

### Task 1: Add Acquisition Inbox Specs to setup.py

**Files:**
- Modify: `setup.py:755-765`

- [ ] **Step 1: Add ACQUISITION_INBOX_SPECS constant**

After the existing `INBOX_SPECS` at line 755, add:

```python
ACQUISITION_INBOX_SPECS = [
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidan"},
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidan.hutchinson"},
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "a.hutchinson"},
]
```

- [ ] **Step 2: Update generate_mailbox_specs to accept mode**

Replace the existing function:

```python
def generate_mailbox_specs(domain_name, count=3, offset=0, mode="client"):
    """Return the standard 3 inbox specs for a domain."""
    specs = ACQUISITION_INBOX_SPECS if mode == "acquisition" else INBOX_SPECS
    return specs[:count]
```

- [ ] **Step 3: Commit**

```bash
git add setup.py
git commit -m "feat: add acquisition inbox specs (Aidan Hutchinson identity)"
```

---

### Task 2: Add Acquisition Domain Filtering to sheets.py

**Files:**
- Modify: `sheets.py:112-127`

- [ ] **Step 1: Add get_acquisition_domains function**

Add after `get_available_domains()`:

```python
def get_acquisition_domains(exclude_keywords=None):
    """Get available domains that ARE headlinetheory domains (for acquisition infrastructure)."""
    if exclude_keywords is None:
        exclude_keywords = EXCLUDED_KEYWORDS
    _, domains = get_all_master_domains()
    available = []
    for d in domains:
        if d["status"].lower() != "available":
            continue
        domain_lower = d["domain"].lower()
        # INCLUDE only domains matching excluded keywords (opposite of client logic)
        if any(kw in domain_lower for kw in exclude_keywords):
            available.append(d)
    return available
```

- [ ] **Step 2: Commit**

```bash
git add sheets.py
git commit -m "feat: add get_acquisition_domains() for THT domain filtering"
```

---

### Task 3: Add Acquisition Mode to Pipeline Config & Interactive Flow

**Files:**
- Modify: `setup.py` — interactive() function (~line 1992) and CLI parsing (~line 2072)

- [ ] **Step 1: Add acquisition interactive function**

Add before the existing `interactive()` function:

```python
def interactive_acquisition(daily_volume=None, group_name=None):
    """Interactive mode for acquisition infrastructure setup."""
    print("\n" + "="*60)
    print("  THT ACQUISITION INFRASTRUCTURE SETUP")
    print("="*60 + "\n")

    # Show THT domain inventory
    try:
        import sheets
        acq_domains = sheets.get_acquisition_domains()
        print(f"  THT domains available: {len(acq_domains)}")
    except Exception:
        print("  Could not check domain inventory")

    if daily_volume is None:
        daily_volume = int(input("  Daily volume for this group: ").strip())

    infra = calculate_infra(daily_volume)

    # Auto-detect next group letter from existing SmartLead tags
    if group_name is None:
        existing_tags = sl_get_all_tags()
        used_letters = set()
        for tag_name in existing_tags:
            # Match "X Group" pattern
            if "group" in tag_name.lower() and "(" in tag_name:
                letter = tag_name.split()[0].strip()
                if len(letter) == 1 and letter.isalpha():
                    used_letters.add(letter.upper())
        next_letter = "A"
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if c not in used_letters:
                next_letter = c
                break
        group_name = f"{next_letter} Group ({daily_volume}/day)"
        print(f"\n  Next group: {group_name}")

    print(f"\n  --- ACQUISITION INFRASTRUCTURE ---")
    print(f"  Group:                 {group_name}")
    print(f"  Daily volume target:   {infra['daily_volume_target']}")
    print(f"  Accounts needed:       {infra['accounts_needed']}")
    print(f"  Domains needed:        {infra['domains_needed']}")
    print(f"  Sender:                Aidan Hutchinson")
    print(f"  Forwarding:            https://theheadlinetheory.com/")

    confirm = input(f"\n  Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return

    config = {
        "mode": "acquisition",
        "client_name": group_name,
        "group_name": group_name,
        "created_date": datetime.now().strftime("%Y-%m-%d"),
        "infrastructure": infra,
        "forwarding_domain": "https://theheadlinetheory.com/",
        "status": "in_progress",
        "steps_completed": [],
    }

    filename = f"acquisition_{group_name.split()[0].lower()}_{datetime.now().strftime('%Y%m%d')}.json"
    config_path = SCRIPT_DIR / "clients" / filename
    save_config(config, config_path)

    print(f"\n  Config saved: {config_path}")
    print(f"  Starting pipeline...\n")

    run_pipeline(config, config_path)
```

- [ ] **Step 2: Add --acquisition CLI flag**

In the CLI parsing section (the `if __name__` block), add handling for `--acquisition`:

```python
        elif sys.argv[1] == "--acquisition":
            vol = int(sys.argv[2]) if len(sys.argv) >= 3 else None
            grp = sys.argv[3] if len(sys.argv) >= 4 else None
            interactive_acquisition(daily_volume=vol, group_name=grp)
```

Also update the usage text to include:
```
  python3 setup.py --acquisition 250                        # Acquisition mode, 250/day group
  python3 setup.py --acquisition 250 "G Group (250/day)"    # Acquisition with explicit group name
```

- [ ] **Step 3: Commit**

```bash
git add setup.py
git commit -m "feat: add --acquisition CLI mode with auto group letter detection"
```

---

### Task 4: Update run_pipeline to Handle Acquisition Mode

**Files:**
- Modify: `setup.py` — run_pipeline() function

- [ ] **Step 1: Update domain pulling (Step 2) to use acquisition domains when mode is acquisition**

Find the domain pulling section in run_pipeline (around line 1000 where `sheets.get_available_domains()` is called). Wrap it to check mode:

```python
        mode = config.get("mode", "client")
        if mode == "acquisition":
            available = sheets.get_acquisition_domains()
            log(f"Found {len(available)} available THT/acquisition domains")
        else:
            available = sheets.get_available_domains()
            log(f"Found {len(available)} available client domains")
```

- [ ] **Step 2: Update inbox creation (Step 5) to use acquisition specs**

Find the `generate_mailbox_specs()` call in the inbox creation step and pass the mode:

```python
specs = generate_mailbox_specs(domain_name, ACCOUNTS_PER_DOMAIN, mode=config.get("mode", "client"))
```

- [ ] **Step 3: Update tagging (Step 10) to use acquisition tag structure**

In the tagging step, after the existing tag creation logic, add acquisition-specific tagging:

```python
            mode = config.get("mode", "client")
            if mode == "acquisition":
                # Acquisition gets 4 tags: Acquisition Inbox, Zapmail, date, group
                group_name = config.get("group_name", client)
                tag_names = ["Acquisition Inbox", "Zapmail", date_tag_name, group_name]
            else:
                # Client gets 3 tags: client name, Zapmail, date
                tag_names = [client, "Zapmail", date_tag_name]

            tag_ids = []
            for tag_name in tag_names:
                tag_id = sl_find_or_create_tag(tag_name, existing_tags=existing_tags)
                if tag_id:
                    tag_ids.append(tag_id)
                    log(f"  Tag: '{tag_name}' → ID {tag_id}")
                else:
                    log(f"  Failed to find/create tag: '{tag_name}'", "WARN")
```

Replace the existing hardcoded 3-tag loop with this mode-aware version.

- [ ] **Step 4: Commit**

```bash
git add setup.py
git commit -m "feat: run_pipeline handles acquisition mode — THT domains, Aidan identity, 4-tag structure"
```

---

### Task 5: Add Acquisition Groups to Dashboard Backend

**Files:**
- Modify: `dashboard.py` — add acquisition overview endpoint

- [ ] **Step 1: Add acquisition group detection logic**

Add a helper function that identifies acquisition accounts by their tags. Since the public API doesn't return tags, use the SmartLead client approach — acquisition accounts are those assigned to group-named clients or have specific client IDs. Actually, the simpler approach: add an `/api/acquisition` endpoint that queries accounts by the known group tag IDs.

Add near the other API functions:

```python
def api_acquisition():
    """Acquisition inbox groups with health metrics."""
    all_accounts = get_all_accounts()
    health_data = get_health_metrics(days=7)
    health_by_email = {h.get("from_email", ""): h for h in health_data}
    warmup_dates = get_warmup_start_dates()

    # Get all SmartLead clients, find group-named ones
    clients = get_clients()
    group_clients = []
    for c in clients:
        name = c.get("name", "")
        if "group" in name.lower() and ("/" in name or "day" in name.lower()):
            group_clients.append(c)

    groups = []
    total_accounts = 0
    for cl in sorted(group_clients, key=lambda x: x.get("name", "")):
        cl_accounts = get_accounts_by_client(cl["id"])
        if not cl_accounts:
            continue

        total_accounts += len(cl_accounts)
        # Calculate health for this group
        group_health = []
        warming = 0
        in_campaign = 0
        smtp_fail = 0
        total_sent = 0
        total_bounced = 0
        total_replied = 0
        flagged_domains = set()
        all_domains = set()

        for acc in cl_accounts:
            email = acc.get("from_email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            all_domains.add(domain)
            h = health_by_email.get(email, {})
            sent = h.get("sent", 0) or 0
            bounced = h.get("bounced", 0) or 0
            replied = h.get("replied", 0) or 0
            total_sent += sent
            total_bounced += bounced
            total_replied += replied

            warmup_rep = acc.get("warmup_details", {}).get("warmup_reputation", 100) if acc.get("warmup_details") else 100
            score = calculate_health_score(
                sent, bounced, replied,
                warmup_rep, 0, False
            )
            if score.get("flagged"):
                flagged_domains.add(domain)
            group_health.append(score.get("score", 100))

            if acc.get("warmup_details", {}).get("status") == "ACTIVE":
                warming += 1
            campaign_count = acc.get("campaign_count", 0) or 0
            if campaign_count > 0:
                in_campaign += 1
            if not acc.get("smtp_connected"):
                smtp_fail += 1

        avg_health = sum(group_health) / len(group_health) if group_health else 100
        avg_bounce = (total_bounced / total_sent * 100) if total_sent > 0 else 0
        avg_reply = (total_replied / total_sent * 100) if total_sent > 0 else 0

        groups.append({
            "id": cl["id"],
            "name": cl["name"],
            "accounts": len(cl_accounts),
            "warming": warming,
            "in_campaign": in_campaign,
            "smtp_failures": smtp_fail,
            "total_sent": total_sent,
            "total_bounced": total_bounced,
            "total_replied": total_replied,
            "avg_bounce_rate": round(avg_bounce, 2),
            "avg_reply_rate": round(avg_reply, 2),
            "health_score": round(avg_health),
            "total_domains": len(all_domains),
            "flagged_domains": len(flagged_domains),
            "flagged_pct": round(len(flagged_domains) / len(all_domains) * 100) if all_domains else 0,
            "needs_attention": len(flagged_domains) / len(all_domains) >= 0.15 if all_domains else False,
        })

    return {
        "groups": groups,
        "total_accounts": total_accounts,
        "total_groups": len(groups),
        "generated_at": datetime.now().isoformat(),
    }
```

- [ ] **Step 2: Register the endpoint in the request handler**

In the `do_GET` handler, add:

```python
            elif path == "/api/acquisition":
                self._json_response(api_acquisition())
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat: add /api/acquisition endpoint — group-based health metrics"
```

---

### Task 6: Add Acquisition Groups UI to Dashboard

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Add Acquisition Groups section to the HTML**

After the client cards section and before the unassigned section, add:

```html
<div class="section" id="acquisition-section" style="display:none;">
    <h2 class="section-title">Acquisition Groups</h2>
    <div class="stats-row" id="acquisition-stats"></div>
    <div class="client-grid" id="acquisition-grid"></div>
</div>
```

- [ ] **Step 2: Add loadAcquisition() and renderAcquisitionGroups() functions**

Add to the JavaScript section:

```javascript
async function loadAcquisition() {
    try {
        const resp = await fetch('/api/acquisition');
        const data = await resp.json();
        if (data.total_groups > 0) {
            document.getElementById('acquisition-section').style.display = 'block';
            document.getElementById('acquisition-stats').innerHTML = `
                <div class="stat-card">
                    <div class="stat-value">${data.total_accounts}</div>
                    <div class="stat-label">Acquisition Inboxes</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${data.total_groups}</div>
                    <div class="stat-label">Active Groups</div>
                </div>
            `;
            renderAcquisitionGroups(data.groups);
        }
    } catch (e) {
        console.error('Failed to load acquisition data:', e);
    }
}

function renderAcquisitionGroups(groups) {
    const grid = document.getElementById('acquisition-grid');
    grid.innerHTML = groups.map(g => {
        const healthColor = g.health_score >= 80 ? '#4ecdc4' : g.health_score >= 50 ? '#ffd93d' : '#ff6b6b';
        const attentionBadge = g.needs_attention ? '<span style="color:#ff6b6b;margin-left:8px;">⚠</span>' : '';
        return `
        <div class="client-card" onclick="openDetail(${g.id}, '${g.name.replace(/'/g, "\\'")}')">
            <div class="client-header">
                <span class="client-name">${g.name}${attentionBadge}</span>
                <span class="health-score" style="background:${healthColor}">${g.health_score}</span>
            </div>
            <div class="client-stats">
                <div>${g.accounts} accounts</div>
                <div>${g.in_campaign} in campaign</div>
                <div>${g.total_domains} domains</div>
                <div>${g.flagged_domains} flagged</div>
            </div>
            <div class="client-metrics">
                <span>Bounce: ${g.avg_bounce_rate}%</span>
                <span>Reply: ${g.avg_reply_rate}%</span>
            </div>
        </div>`;
    }).join('');
}
```

- [ ] **Step 3: Call loadAcquisition() in the main load flow**

Find the existing `loadOverview()` or main load function and add `loadAcquisition()` call alongside the other data loads.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: add Acquisition Groups section to dashboard UI"
```

---

### Task 7: Create Acquisition SmartLead Clients for Existing Groups

**Files:**
- None (runtime operation)

- [ ] **Step 1: Assign existing acquisition accounts to group clients**

The existing 593 unassigned accounts need to be assigned to SmartLead clients matching their group tags. Since we can't read tags from the public API, we need to check the internal API or use domain-based mapping.

First, check if group-named clients already exist. If not, create them for A-F. Then assign accounts based on whatever grouping logic maps them (this may require reading tags from the internal API or manual mapping).

This step is a one-time migration that should be scripted separately — add it as a note in the plan but don't automate it in the pipeline since it requires knowing which accounts belong to which group.

- [ ] **Step 2: Document the migration approach**

The migration script should:
1. Query all accounts without client_id
2. For each, read its tags via the internal API (if possible) or use the existing tag assignments
3. Match group tag → group client ID
4. Call save-management-details with the client_id (preserving existing tags)

---
