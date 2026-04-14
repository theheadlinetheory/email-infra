# Pipeline Retry/Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic retry with backoff to the pipeline engine, per-domain status visibility and manual retry/skip controls to the dashboard frontend, and Slack notifications when a pipeline genuinely fails after all retries.

**Architecture:** The pipeline engine (`pipeline.py`) gets a retry wrapper that calls step executors multiple times with configurable backoff before ever setting error status. The dashboard backend (`dashboard.py`) gets two new POST endpoints for manual retry and skip. The dashboard frontend (`dashboard.html`) gets an expanded pipeline card with per-domain breakdown, retry/skip buttons, and auto-polling. A Slack webhook fires only after all automatic retries are exhausted.

**Tech Stack:** Python 3 (pipeline engine + HTTP server), vanilla JS (dashboard frontend), Slack Incoming Webhook, Supabase (pipeline persistence)

**Spec:** `docs/superpowers/specs/2026-04-14-pipeline-retry-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `pipeline.py` | Modify | Add retry policies, `_run_step_with_retry()`, `notify_pipeline_error()`, fix `_mark_all_domains_complete()` |
| `dashboard.py` | Modify | Add `api_pipeline_retry()`, `api_pipeline_skip_step()`, enhance `api_pipeline_active()` with per-domain data, wire new POST routes |
| `dashboard.html` | Modify | Expanded pipeline card with domain table, retry/skip buttons, auto-polling |
| `.env` | Modify | Add `SLACK_WEBHOOK_URL` |

---

### Task 1: Add Retry Policies and Per-Domain Attempt Tracking to Pipeline Engine

**Files:**
- Modify: `pipeline.py:66-72` (constants section)
- Modify: `pipeline.py:113-118` (`_mark_all_domains_complete`)
- Modify: `pipeline.py:154-190` (`create_pipeline` — domain init)

- [ ] **Step 1: Add retry policy constants after existing constants**

In `pipeline.py`, after line 73 (`ZAPMAIL_ACTIVATION_TIMEOUT_S = 30 * 60`), add:

```python
# Retry policies: {step_name: (max_attempts, [wait_seconds_between_attempts])}
RETRY_POLICIES = {
    "connect_zapmail":      (3, [0, 1800, 1800]),    # 30 min poll window each
    "create_mailboxes":     (3, [0, 300, 600]),       # 5 min, then 10 min
    "export_to_smartlead":  (3, [0, 900, 900]),       # 15 min poll window each
    "enable_warmup":        (3, [0, 120, 300]),       # 2 min, then 5 min
}
DEFAULT_RETRY_POLICY = (2, [0, 120])                  # 2 attempts, 2 min wait
```

- [ ] **Step 2: Fix `_mark_all_domains_complete` to skip already-complete domains**

Replace the existing function at line 113:

```python
def _mark_all_domains_complete(pipeline, step_name):
    """Mark pending/error domains as complete for a given step.
    Skips domains already marked complete for this step (preserves retry state).
    """
    for info in pipeline["domains"].values():
        if info["step"] == step_name and info["step_status"] == "complete":
            continue
        info["step"] = step_name
        info["step_status"] = "complete"
```

- [ ] **Step 3: Add per-domain attempt tracking to `create_pipeline`**

In the `create_pipeline` function, update the domain init dict (around line 178) to include attempt tracking:

```python
    for d in domains:
        pipeline["domains"][d["domain"]] = {
            "provider": d.get("provider", ""),
            "row_number": d.get("row_number"),
            "step": steps[0],
            "step_status": "pending",
            "zapmail_domain_id": None,
            "mailbox_ids": [],
            "smartlead_account_ids": [],
            "error": None,
            "attempt": 1,
            "max_attempts": 3,
            "step_history": [],
        }
```

- [ ] **Step 4: Verify no syntax errors**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "import pipeline; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add pipeline.py
git commit -m "feat(pipeline): add retry policies, per-domain attempt tracking, fix _mark_all_domains_complete"
```

---

### Task 2: Implement `_run_step_with_retry()` Wrapper

**Files:**
- Modify: `pipeline.py:832-871` (`run_pipeline_steps`)

- [ ] **Step 1: Add `_run_step_with_retry` function before `run_pipeline_steps`**

Insert before `run_pipeline_steps` (line 832):

```python
def _run_step_with_retry(pipeline, step_name):
    """Execute a step with automatic retry and backoff.

    Returns True if all domains passed, False if any domain still errored
    after exhausting all retry attempts.
    """
    policy = RETRY_POLICIES.get(step_name, DEFAULT_RETRY_POLICY)
    max_attempts, waits = policy

    executor = STEP_EXECUTORS[step_name]

    for attempt_num in range(1, max_attempts + 1):
        # Update attempt info on pending/error domains
        for info in pipeline["domains"].values():
            if info["step_status"] in ("pending", "error", "retry"):
                info["attempt"] = attempt_num
                info["max_attempts"] = max_attempts

        # Update pipeline retry info for frontend display
        pipeline["retry_info"] = {
            "step": step_name,
            "attempt": attempt_num,
            "max_attempts": max_attempts,
        }
        pipeline["updated_at"] = datetime.now().isoformat()
        save_pipeline(pipeline)

        # Run the step executor
        success = executor(pipeline)
        save_pipeline(pipeline)

        if success:
            pipeline.pop("retry_info", None)
            return True

        # Check if any domains actually errored (vs waiting states)
        errored = [
            d for d, info in pipeline["domains"].items()
            if info.get("step_status") in ("error", "retry")
        ]
        if not errored:
            # Step returned False but no domain errors — might be a wait state
            pipeline.pop("retry_info", None)
            return False

        # If we have more attempts, log the failure and wait
        if attempt_num < max_attempts:
            wait_secs = waits[attempt_num] if attempt_num < len(waits) else waits[-1]
            log.info(
                "[RETRY] %s attempt %d/%d failed for %d domains, waiting %ds",
                step_name, attempt_num, max_attempts, len(errored), wait_secs,
            )

            # Record attempt in step_history
            for d_name in errored:
                info = pipeline["domains"][d_name]
                info["step_history"].append({
                    "attempt": attempt_num,
                    "result": "error",
                    "message": info.get("error", ""),
                    "at": datetime.now().isoformat(),
                })
                # Reset for next attempt
                info["step_status"] = "pending"
                info["error"] = None

            save_pipeline(pipeline)
            time.sleep(wait_secs)
        else:
            # Final attempt exhausted — record in history
            for d_name in errored:
                info = pipeline["domains"][d_name]
                info["step_history"].append({
                    "attempt": attempt_num,
                    "result": "error",
                    "message": info.get("error", ""),
                    "at": datetime.now().isoformat(),
                })

    pipeline.pop("retry_info", None)
    return False
```

- [ ] **Step 2: Update `run_pipeline_steps` to use the retry wrapper**

Replace the executor call section in `run_pipeline_steps` (the for loop body inside the try block):

```python
def run_pipeline_steps(pipeline):
    """Execute pipeline steps from current position until blocked or complete.
    Acquires a per-pipeline lock to prevent concurrent execution.
    """
    lock = _get_pipeline_lock(pipeline["id"])
    if not lock.acquire(blocking=False):
        log.info("Pipeline %s already running, skipping", pipeline["id"])
        return

    try:
        steps = pipeline["steps"]
        current_idx = steps.index(pipeline["current_step"])

        for i in range(current_idx, len(steps)):
            step_name = steps[i]
            pipeline["current_step"] = step_name
            pipeline["updated_at"] = datetime.now().isoformat()
            save_pipeline(pipeline)

            success = _run_step_with_retry(pipeline, step_name)
            save_pipeline(pipeline)

            if not success:
                if pipeline["status"] == "awaiting_removal":
                    return
                if step_name == "wait_for_warmup":
                    return
                # All retries exhausted — error
                pipeline["status"] = "error"
                save_pipeline(pipeline)
                notify_pipeline_error(pipeline)
                return

        # All steps complete
        if pipeline["status"] != "complete":
            pipeline["status"] = "complete"
            pipeline["completed_at"] = datetime.now().isoformat()
            save_pipeline(pipeline)
    finally:
        lock.release()
```

- [ ] **Step 3: Verify no syntax errors**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "import pipeline; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add pipeline.py
git commit -m "feat(pipeline): add _run_step_with_retry wrapper with automatic backoff"
```

---

### Task 3: Add Slack Notification on True Failure

**Files:**
- Modify: `pipeline.py` (top-level, add `notify_pipeline_error` function)
- Modify: `.env` (add `SLACK_WEBHOOK_URL`)

- [ ] **Step 1: Add SLACK_WEBHOOK_URL to environment loading**

In `pipeline.py`, after the existing imports and constants (after the `RETRY_POLICIES` block added in Task 1), add:

```python
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
```

- [ ] **Step 2: Add `notify_pipeline_error` function**

Add after the `SLACK_WEBHOOK_URL` line:

```python
def notify_pipeline_error(pipeline):
    """Send Slack notification when a pipeline fails after all automatic retries."""
    if not SLACK_WEBHOOK_URL:
        log.warning("[SLACK] No SLACK_WEBHOOK_URL configured, skipping notification")
        return

    step = pipeline.get("current_step", "unknown")
    client = pipeline.get("client_name", "unknown")
    retry_info = pipeline.get("retry_info", {})
    max_attempts = retry_info.get("max_attempts", "?")

    # Collect failed domains
    failed = []
    passed = 0
    for domain, info in pipeline.get("domains", {}).items():
        if info.get("step_status") == "error":
            failed.append(f"  • {domain}: {info.get('error', 'Unknown error')}")
        elif info.get("step_status") == "complete":
            passed += 1

    total = len(pipeline.get("domains", {}))
    failed_text = "\n".join(failed) if failed else "  (no domain-level errors captured)"

    policy = RETRY_POLICIES.get(step, DEFAULT_RETRY_POLICY)
    attempts_used = policy[0]

    text = (
        f":rotating_light: *Pipeline Error: {client}*\n"
        f"*Step:* {step} ({attempts_used}/{attempts_used} attempts exhausted)\n\n"
        f"*Failed domains:*\n{failed_text}\n\n"
        f"{passed}/{total} domains completed successfully."
    )

    try:
        requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("[SLACK] Failed to send notification: %s", e)
```

- [ ] **Step 3: Add placeholder to .env**

Append to `/Users/aidanhutchinson/email-infra/.env`:

```
SLACK_WEBHOOK_URL=
```

(Will be filled in after creating the Slack channel and webhook in Task 7.)

- [ ] **Step 4: Verify no syntax errors**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "import pipeline; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add pipeline.py .env
git commit -m "feat(pipeline): add Slack notification on true pipeline failure"
```

---

### Task 4: Add Backend API Endpoints for Retry and Skip

**Files:**
- Modify: `dashboard.py:1384-1415` (pipeline API section)
- Modify: `dashboard.py:2537-2545` (POST route handler)

- [ ] **Step 1: Add `api_pipeline_retry` function**

In `dashboard.py`, after the `api_pipeline_detail` function (after line 1414), add:

```python
def api_pipeline_retry(body):
    """Retry failed domains on the current step of an errored pipeline."""
    pipeline_id = body.get("pipeline_id", "")
    retry_domains = body.get("domains", [])  # empty = retry all failed

    if not pipeline_id:
        return {"error": "pipeline_id required"}

    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    if p["status"] != "error":
        return {"error": f"Pipeline is '{p['status']}', not 'error' — cannot retry"}

    # Find domains to retry
    retrying = []
    for domain, info in p["domains"].items():
        if info.get("step_status") == "error":
            if retry_domains and domain not in retry_domains:
                continue
            info["step_status"] = "pending"
            info["error"] = None
            info["attempt"] = 1
            retrying.append(domain)

    if not retrying:
        return {"error": "No failed domains to retry"}

    p["status"] = "running"
    p["errors"] = []  # clear stale error list
    p["updated_at"] = datetime.now().isoformat()
    save_pipeline(p)

    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()

    return {"pipeline_id": p["id"], "status": "running", "retrying_domains": retrying}
```

- [ ] **Step 2: Add `api_pipeline_skip_step` function**

Immediately after `api_pipeline_retry`, add:

```python
def api_pipeline_skip_step(body):
    """Skip the current step and advance to the next one."""
    pipeline_id = body.get("pipeline_id", "")

    if not pipeline_id:
        return {"error": "pipeline_id required"}

    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    if p["status"] != "error":
        return {"error": f"Pipeline is '{p['status']}', not 'error' — cannot skip"}

    current_step = p["current_step"]
    steps = p["steps"]
    current_idx = steps.index(current_step)

    if current_idx >= len(steps) - 1:
        return {"error": "Already on the last step — cannot skip"}

    # Mark all domains as complete for skipped step, log the skip
    for domain, info in p["domains"].items():
        info["step_history"].append({
            "attempt": info.get("attempt", 0),
            "result": "skipped",
            "message": f"Step '{current_step}' manually skipped",
            "at": datetime.now().isoformat(),
        })
        info["step"] = current_step
        info["step_status"] = "complete"
        info["error"] = None

    next_step = steps[current_idx + 1]
    p["current_step"] = next_step
    p["status"] = "running"
    p["errors"] = []
    p["updated_at"] = datetime.now().isoformat()
    save_pipeline(p)

    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()

    return {
        "pipeline_id": p["id"],
        "skipped_step": current_step,
        "next_step": next_step,
        "warning": "Step skipped — some domains may have incomplete setup",
    }
```

- [ ] **Step 3: Enhance `api_pipeline_active` to include per-domain data**

Replace the `api_pipeline_active` function:

```python
def api_pipeline_active():
    """List all pipelines with per-domain status."""
    try:
        all_p = load_all_pipelines()
    except Exception as e:
        print(f"WARN: Could not load pipelines: {e}")
        all_p = []
    result = []
    for p in all_p:
        # Build per-domain summary
        domain_details = {}
        for domain, info in p.get("domains", {}).items():
            domain_details[domain] = {
                "step": info.get("step", ""),
                "step_status": info.get("step_status", ""),
                "error": info.get("error"),
                "attempt": info.get("attempt", 1),
                "max_attempts": info.get("max_attempts", 3),
                "step_history": info.get("step_history", []),
            }

        result.append({
            "id": p["id"],
            "type": p["type"],
            "client_name": p["client_name"],
            "status": p["status"],
            "current_step": p.get("current_step", ""),
            "steps": p.get("steps", []),
            "domains": list(p["domains"].keys()),
            "domain_details": domain_details,
            "created_at": p.get("created_at", ""),
            "updated_at": p.get("updated_at", ""),
            "errors": p.get("errors", []),
            "pending_removals": p.get("pending_removals", {}),
            "retry_info": p.get("retry_info"),
        })
    result.sort(key=lambda p: p["created_at"], reverse=True)
    return {"pipelines": result}
```

- [ ] **Step 4: Wire up POST routes**

In the `do_POST` method, after the `elif path == "/api/pipeline/new-acquisition":` block (around line 2545), add:

```python
        elif path == "/api/pipeline/retry":
            result = api_pipeline_retry(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/skip-step":
            result = api_pipeline_skip_step(body)
            self._json_response(result, 400 if "error" in result else 200)
```

- [ ] **Step 5: Verify no syntax errors**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "import dashboard; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add dashboard.py
git commit -m "feat(dashboard): add pipeline retry and skip-step API endpoints, per-domain data in active pipelines"
```

---

### Task 5: Frontend — Expanded Pipeline Card with Per-Domain Table

**Files:**
- Modify: `dashboard.html:1561-1662` (`renderPipelines` function)

- [ ] **Step 1: Replace the `renderPipelines` function**

Replace the entire `renderPipelines` function (lines 1561-1662) with:

```javascript
function renderPipelines() {
    const pipelines = pipelineData.pipelines || [];

    const active = pipelines.filter(p => p.status === 'running' || p.status === 'awaiting_removal');
    const badge = document.getElementById('pipeline-badge');
    if (active.length > 0) {
        badge.style.display = 'inline';
        badge.textContent = active.length + ' active';
    } else {
        badge.style.display = 'none';
    }

    if (pipelines.length === 0) {
        document.getElementById('pipelines-content').innerHTML = '<div style="text-align:center;color:#888;padding:40px;">No pipelines yet. Start one from the SmartLead tab.</div>';
        return;
    }

    const stepLabels = {
        claim_domains: 'Claim Domains',
        set_dns: 'Set DNS',
        connect_zapmail: 'Connect ZapMail',
        create_mailboxes: 'Create Mailboxes',
        upload_photos: 'Upload Photos',
        tag_and_configure: 'Tag & Configure',
        export_to_smartlead: 'Export to SmartLead',
        enable_warmup: 'Enable Warmup',
        smartlead_tags: 'SmartLead Tags',
        export_csv: 'Export CSV',
        gcal_rotation: 'Schedule Rotation',
        wait_for_warmup: 'Waiting for Warmup',
        check_campaigns: 'Check Campaigns',
        remove_old: 'Remove Old Inboxes',
        cleanup: 'Cleanup',
    };

    let html = '';
    pipelines.forEach(p => {
        const statusColor = p.status === 'complete' ? '#4ecdc4' : p.status === 'error' ? '#ff6b6b' : p.status === 'awaiting_removal' ? '#ffd93d' : '#7c4dff';
        const statusLabel = p.status === 'awaiting_removal' ? 'Awaiting Removal' : p.status.charAt(0).toUpperCase() + p.status.slice(1);

        // Retry info label
        let stepSuffix = '';
        if (p.retry_info && p.status === 'running') {
            stepSuffix = ` (attempt ${p.retry_info.attempt}/${p.retry_info.max_attempts})`;
        }

        html += `<div style="background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:16px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <div>
                    <span style="font-size:16px;font-weight:600;">${p.client_name}</span>
                    <span style="font-size:13px;color:#888;margin-left:12px;">${p.type === 'new_setup' ? 'New Setup' : p.type === 'acquisition' ? 'Acquisition' : 'Replacement'}</span>
                </div>
                <span style="color:${statusColor};font-weight:500;">${statusLabel}</span>
            </div>
            <div style="font-size:13px;color:#888;margin-bottom:8px;">Domains: ${p.domains.length}</div>
            <div style="font-size:12px;color:#888;">Started: ${new Date(p.created_at).toLocaleString()}</div>`;

        // Step progress bar
        if (p.status !== 'complete') {
            const allSteps = p.steps || [];
            const currentIdx = allSteps.indexOf(p.current_step);
            html += '<div style="display:flex;gap:4px;margin-top:12px;flex-wrap:wrap;">';
            allSteps.forEach((s, i) => {
                let color, textColor;
                if (i < currentIdx) {
                    color = '#4ecdc4'; textColor = '#fff';
                } else if (i === currentIdx) {
                    color = p.status === 'error' ? '#ff6b6b' : '#7c4dff';
                    textColor = '#fff';
                } else {
                    color = '#333'; textColor = '#666';
                }
                const label = (stepLabels[s] || s) + (i === currentIdx ? stepSuffix : '');
                html += `<div style="background:${color};padding:4px 10px;border-radius:4px;font-size:11px;color:${textColor};" title="${label}">${label}</div>`;
            });
            html += '</div>';
        }

        // Per-domain table (show when error, or running with errors)
        const dd = p.domain_details || {};
        const hasErrors = Object.values(dd).some(d => d.step_status === 'error');
        if ((p.status === 'error' || hasErrors) && Object.keys(dd).length > 0) {
            html += `<div style="margin-top:12px;background:#0a1628;border-radius:8px;padding:12px;overflow-x:auto;">
                <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <thead><tr>
                    <th style="text-align:left;padding:6px 8px;color:#888;border-bottom:1px solid #0f3460;">Domain</th>
                    <th style="text-align:left;padding:6px 8px;color:#888;border-bottom:1px solid #0f3460;">Status</th>
                    <th style="text-align:left;padding:6px 8px;color:#888;border-bottom:1px solid #0f3460;">Error</th>
                    <th style="text-align:left;padding:6px 8px;color:#888;border-bottom:1px solid #0f3460;">Attempts</th>
                </tr></thead><tbody>`;

            for (const [domain, detail] of Object.entries(dd)) {
                const statusBadge = detail.step_status === 'complete'
                    ? '<span style="color:#4ecdc4;font-weight:500;">Complete</span>'
                    : detail.step_status === 'error'
                    ? '<span style="color:#ff6b6b;font-weight:500;">Error</span>'
                    : detail.step_status === 'pending'
                    ? '<span style="color:#888;">Pending</span>'
                    : '<span style="color:#7c4dff;font-weight:500;">Running</span>';

                const errorText = detail.error || '—';
                const attemptText = detail.step_status === 'error'
                    ? `${detail.attempt}/${detail.max_attempts} failed`
                    : detail.step_status === 'complete' ? '—' : `${detail.attempt}/${detail.max_attempts}`;

                html += `<tr>
                    <td style="padding:6px 8px;border-bottom:1px solid #0a1628;color:#ddd;">${domain}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid #0a1628;">${statusBadge}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid #0a1628;color:#ff9999;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;">${errorText}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid #0a1628;color:#888;">${attemptText}</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        // Retry / Skip buttons (error state only)
        if (p.status === 'error') {
            const failedDomains = Object.entries(dd).filter(([_, d]) => d.step_status === 'error').map(([name]) => name);
            html += `<div style="margin-top:12px;display:flex;gap:12px;align-items:center;">
                <button onclick="retryPipeline('${p.id}')" style="background:#4ecdc4;color:#1a1a2e;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;">Retry Failed (${failedDomains.length})</button>
                <button onclick="skipPipelineStep('${p.id}','${p.current_step}')" style="background:none;color:#ff6b6b;border:1px solid #5c1a1a;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;">Skip Step</button>
            </div>`;
        }

        // Awaiting removal section (existing)
        if (p.status === 'awaiting_removal' && p.pending_removals) {
            html += '<div style="background:#4a1a1a;border:1px solid #8b3a3a;border-radius:8px;padding:12px;margin-top:12px;">';
            html += '<div style="color:#ff6b6b;font-weight:600;margin-bottom:8px;">Inboxes need removal from campaigns</div>';
            for (const [email, camps] of Object.entries(p.pending_removals)) {
                html += `<div style="margin-bottom:8px;">
                    <div style="font-size:13px;color:#ffaaaa;">${email} is in ${camps.length} campaign(s):</div>`;
                camps.forEach(c => {
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 4px 16px;font-size:12px;">
                        <span style="color:#888;">${c.campaign_name}</span>
                        <button onclick="removeFromCampaign('${email}',${c.campaign_id})" style="background:#5c1a1a;color:#ff6b6b;border:1px solid #8b3a3a;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:11px;">Remove</button>
                    </div>`;
                });
                html += `<button onclick="removeFromAllCampaigns('${email}')" style="background:#5c1a1a;color:#ff6b6b;border:1px solid #8b3a3a;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:4px;">Remove from all campaigns</button>`;
                html += '</div>';
            }
            html += '</div>';
        }

        // Assign to Client button for generic pipelines
        const isGeneric = p.client_name && p.client_name.toLowerCase().startsWith('generic');
        if (isGeneric && (p.status === 'complete' || p.status === 'running')) {
            html += `<div style="margin-top:12px;display:flex;justify-content:flex-end;">
                <button onclick="event.stopPropagation();openAssignModal('${p.id}','${p.client_name.replace(/'/g, "\\'")}')" style="background:#7c4dff;color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:500;font-size:13px;">Assign to Client</button>
            </div>`;
        }

        if (p.errors && p.errors.length > 0) {
            html += '<div style="margin-top:8px;font-size:12px;color:#ff6b6b;">';
            p.errors.forEach(e => { html += '<div>' + e + '</div>'; });
            html += '</div>';
        }

        html += '</div>';
    });

    document.getElementById('pipelines-content').innerHTML = html;
}
```

- [ ] **Step 2: Verify the HTML renders (no syntax errors in JS)**

Open the dashboard in a browser and navigate to the Pipelines tab. Existing pipelines should render without JS console errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add dashboard.html
git commit -m "feat(dashboard): expanded pipeline card with per-domain table, retry/skip buttons"
```

---

### Task 6: Frontend — Retry/Skip JS Functions and Auto-Polling

**Files:**
- Modify: `dashboard.html` (after `renderPipelines`, around line 1662)

- [ ] **Step 1: Add `retryPipeline` and `skipPipelineStep` functions**

After the `removeFromAllCampaigns` function (around line 1686), add:

```javascript
async function retryPipeline(pipelineId, domains) {
    try {
        const resp = await fetch('/api/pipeline/retry', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({pipeline_id: pipelineId, domains: domains || []})
        });
        const result = await resp.json();
        if (result.error) {
            alert('Retry failed: ' + result.error);
        } else {
            loadPipelines();
            startPipelinePolling();
        }
    } catch(e) { alert('Error: ' + e.message); }
}

async function skipPipelineStep(pipelineId, stepName) {
    const stepLabels = {
        claim_domains: 'Claim Domains', set_dns: 'Set DNS', connect_zapmail: 'Connect ZapMail',
        create_mailboxes: 'Create Mailboxes', upload_photos: 'Upload Photos',
        tag_and_configure: 'Tag & Configure', export_to_smartlead: 'Export to SmartLead',
        enable_warmup: 'Enable Warmup', smartlead_tags: 'SmartLead Tags',
        export_csv: 'Export CSV', gcal_rotation: 'Schedule Rotation',
    };
    const label = stepLabels[stepName] || stepName;
    if (!confirm(`Skip "${label}"? Domains that failed this step may have incomplete setup. This should only be used as a last resort.`)) return;
    try {
        const resp = await fetch('/api/pipeline/skip-step', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({pipeline_id: pipelineId})
        });
        const result = await resp.json();
        if (result.error) {
            alert('Skip failed: ' + result.error);
        } else {
            alert('Skipped ' + label + '. Pipeline moving to: ' + (stepLabels[result.next_step] || result.next_step));
            loadPipelines();
            startPipelinePolling();
        }
    } catch(e) { alert('Error: ' + e.message); }
}

// --- Auto-polling for running pipelines ---
let pipelinePollingInterval = null;

function startPipelinePolling() {
    if (pipelinePollingInterval) return; // already polling
    pipelinePollingInterval = setInterval(async () => {
        const pipelines = (pipelineData || {}).pipelines || [];
        const hasRunning = pipelines.some(p => p.status === 'running');
        if (!hasRunning) {
            stopPipelinePolling();
            return;
        }
        try {
            const resp = await fetch('/api/pipeline/active');
            pipelineData = await resp.json();
            renderPipelines();
        } catch(e) { /* silent — will retry next interval */ }
    }, 10000);
}

function stopPipelinePolling() {
    if (pipelinePollingInterval) {
        clearInterval(pipelinePollingInterval);
        pipelinePollingInterval = null;
    }
}
```

- [ ] **Step 2: Start polling automatically when pipelines tab loads**

Find the existing `loadPipelines` function and add `startPipelinePolling()` at the end:

```javascript
async function loadPipelines() {
    document.getElementById('pipelines-loading').style.display = 'block';
    document.getElementById('pipelines-content').innerHTML = '';
    try {
        const resp = await fetch('/api/pipeline/active');
        pipelineData = await resp.json();
        renderPipelines();
        // Start polling if any pipeline is running
        const hasRunning = (pipelineData.pipelines || []).some(p => p.status === 'running');
        if (hasRunning) startPipelinePolling();
    } catch(err) {
        document.getElementById('pipelines-content').innerHTML = 'Error: ' + err.message;
    }
    document.getElementById('pipelines-loading').style.display = 'none';
}
```

- [ ] **Step 3: Verify auto-polling works**

Open the dashboard, go to Pipelines tab with a running pipeline. Browser dev tools Network tab should show `/api/pipeline/active` requests every 10 seconds. When pipeline completes or errors, polling should stop.

- [ ] **Step 4: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add dashboard.html
git commit -m "feat(dashboard): add retry/skip JS functions and auto-polling for running pipelines"
```

---

### Task 7: Create Slack Channel and Configure Webhook

**Files:**
- Modify: `.env` (fill in `SLACK_WEBHOOK_URL`)

- [ ] **Step 1: Create `#tht-infra-alerts` Slack channel**

Use Slack MCP tools to create the channel and add Aidan and Lars.

- [ ] **Step 2: Create incoming webhook for the channel**

Set up an incoming webhook app pointed at `#tht-infra-alerts`. Copy the webhook URL.

- [ ] **Step 3: Add webhook URL to `.env`**

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXXXX/XXXXX/XXXXX
```

- [ ] **Step 4: Test the webhook**

Run:
```bash
cd /Users/aidanhutchinson/email-infra
python3 -c "
import requests, os
from pathlib import Path
for line in (Path('.env')).read_text().splitlines():
    if 'SLACK_WEBHOOK_URL' in line and '=' in line:
        url = line.split('=',1)[1].strip()
        if url:
            r = requests.post(url, json={'text': 'Test: THT infra alerts webhook connected.'}, timeout=10)
            print('Status:', r.status_code, r.text)
        else:
            print('No URL set yet')
        break
"
```
Expected: `Status: 200 ok`

- [ ] **Step 5: Commit**

```bash
cd /Users/aidanhutchinson/email-infra
git add .env
git commit -m "feat: configure Slack webhook for pipeline error notifications"
```

---

### Task 8: Integration Smoke Test

**Files:** None (verification only)

- [ ] **Step 1: Verify pipeline engine loads cleanly**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "from pipeline import run_pipeline_steps, _run_step_with_retry, notify_pipeline_error, RETRY_POLICIES; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 2: Verify dashboard loads cleanly**

Run: `cd /Users/aidanhutchinson/email-infra && python3 -c "from dashboard import api_pipeline_retry, api_pipeline_skip_step; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Verify retry API rejects non-error pipelines**

Start the dashboard server, then test:
```bash
curl -s -X POST http://localhost:8080/api/pipeline/retry \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id":"nonexistent"}' | python3 -m json.tool
```
Expected: `{"error": "Pipeline not found"}`

- [ ] **Step 4: Manual end-to-end test**

1. Open the dashboard, go to Pipelines tab
2. If there's an errored pipeline, verify the per-domain table shows with status badges
3. Click "Retry Failed" — verify the pipeline goes back to "running" and auto-polling starts
4. If no errored pipeline exists, verify existing complete/running pipelines render correctly with the new card format

- [ ] **Step 5: Commit any fixes found during testing**

```bash
cd /Users/aidanhutchinson/email-infra
git add -A
git commit -m "fix: address issues found during integration smoke test"
```
