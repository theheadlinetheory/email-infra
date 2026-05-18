// ── Pipelines (Setup + Old Pipeline Tab) ──

var STEP_LABELS = {
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

function stepStatusIcon(status) {
    if (status === 'completed') return '&#10003;';
    if (status === 'running') return '&#9679;';
    if (status === 'failed') return '&#10007;';
    return '&#9675;';
}

function stepStatusColor(status) {
    if (status === 'completed' || status === 'running') return 'var(--accent)';
    if (status === 'failed') return 'var(--red)';
    return 'var(--text-muted)';
}

function renderSetupPipelineSteps(steps) {
    return steps.map((s, i) => {
        var icon = stepStatusIcon(s.status);
        var cls = s.status || 'pending';
        var connector = i < steps.length - 1
            ? '<div class="pill-connector ' + (s.status === 'completed' ? 'done' : 'pending') + '"></div>'
            : '';
        var shortName = s.name.replace('Connect Domains', 'Connect')
            .replace('Create Inboxes', 'Inboxes')
            .replace('Profile Photos', 'Photos')
            .replace('SmartLead Export', 'Export')
            .replace('Tag & Assign', 'Tag')
            .replace('Enable Warmup', 'Warmup');
        return '<span class="pill-step ' + cls + '"><span class="pill-icon">' + icon + '</span>' + shortName + '</span>' + connector;
    }).join('');
}

function setupPipelineStatusLine(p) {
    if (p.status === 'completed') return 'Complete';
    if (p.status === 'failed') {
        var failed = p.steps.find(s => s.status === 'failed');
        return failed ? 'Failed: ' + (failed.error || failed.name) : 'Failed';
    }
    var running = p.steps.find(s => s.status === 'running');
    if (running) return running.name + '... ' + running.progress + '/' + running.total;
    return p.status;
}

function renderSetupPipelines(pipelines) {
    var grid = document.getElementById('setup-pipeline-grid');
    if (!pipelines || !pipelines.length) {
        document.getElementById('setup-pipeline-section').style.display = 'none';
        return;
    }
    document.getElementById('setup-pipeline-section').style.display = '';
    grid.innerHTML = pipelines.map(p => {
        var statusLine = setupPipelineStatusLine(p);
        var retryBtn = p.status === 'failed'
            ? '<button onclick="event.stopPropagation();retrySetupPipeline(\'' + p.id + '\')" style="margin-top:8px;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;">Retry</button>'
            : '';
        return '<div class="client-card" onclick="showSetupPipelineDetail(\'' + p.id + '\')" style="cursor:pointer;">' +
            '<div class="cc-header"><span class="cc-name">' + p.name + '</span>' +
            '<span class="badge" style="background:' + (p.status === 'completed' ? 'var(--accent-bg)' : p.status === 'failed' ? '#fef2f2' : 'var(--accent-bg)') + ';color:' + (p.status === 'completed' ? 'var(--accent)' : p.status === 'failed' ? 'var(--red)' : 'var(--accent)') + ';">' + p.type + '</span></div>' +
            '<div class="pill-stepper">' + renderSetupPipelineSteps(p.steps) + '</div>' +
            '<div class="pipeline-status-line">' + statusLine + '</div>' +
            retryBtn +
        '</div>';
    }).join('');
}

async function openNewPipelineModal() {
    newSetupPipelineType = 'generic';
    var suggestedName = 'Generic A';
    try {
        var resp = await fetch('/api/next-generic-name');
        var data = await resp.json();
        suggestedName = data.name || 'Generic A';
    } catch(e) {}

    var overlay = document.createElement('div');
    overlay.className = 'pipeline-modal-overlay';
    overlay.id = 'setup-pipeline-modal';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    overlay.innerHTML = '<div class="pipeline-modal">' +
        '<h3>New Infrastructure Pipeline</h3>' +
        '<label>Type</label>' +
        '<div class="type-pills">' +
            '<span class="type-pill active" onclick="selectSetupPipelineType(\'generic\',this)" data-type="generic">Generic Group</span>' +
            '<span class="type-pill" onclick="selectSetupPipelineType(\'client\',this)" data-type="client">Client</span>' +
            '<span class="type-pill" onclick="selectSetupPipelineType(\'acquisition\',this)" data-type="acquisition">Acquisition</span>' +
        '</div>' +
        '<label>Name</label>' +
        '<input type="text" id="setup-pipeline-name" value="' + suggestedName + '" placeholder="Generic A">' +
        '<label>Domains (one per line)</label>' +
        '<textarea id="setup-pipeline-domains" placeholder="domain1.info&#10;domain2.info&#10;domain3.info"></textarea>' +
        '<label>Sender</label>' +
        '<select id="setup-pipeline-sender">' +
            '<option value="sean_reynolds">Sean Reynolds</option>' +
            '<option value="aidan_hutchinson">Aidan Hutchinson</option>' +
            '<option value="lars_matthys">Lars Matthys</option>' +
        '</select>' +
        '<button class="btn-start" onclick="startSetupPipeline()">Start Pipeline</button>' +
    '</div>';
    document.body.appendChild(overlay);
}

function selectSetupPipelineType(type, el) {
    newSetupPipelineType = type;
    document.querySelectorAll('.type-pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    if (type === 'generic') {
        fetch('/api/next-generic-name').then(r => r.json()).then(d => {
            document.getElementById('setup-pipeline-name').value = d.name || '';
        }).catch(() => {});
    } else {
        document.getElementById('setup-pipeline-name').value = '';
        document.getElementById('setup-pipeline-name').placeholder = type === 'client' ? 'Client Name' : 'Group Name';
    }
    var sel = document.getElementById('setup-pipeline-sender');
    sel.value = type === 'acquisition' ? 'aidan_hutchinson' : 'sean_reynolds';
}

async function startSetupPipeline() {
    var name = document.getElementById('setup-pipeline-name').value.trim();
    var domains = document.getElementById('setup-pipeline-domains').value.trim();
    var sender = document.getElementById('setup-pipeline-sender').value;
    if (!name || !domains) { alert('Name and domains are required'); return; }
    try {
        var data = await apiPost('/api/setup-pipeline/create', { type: newSetupPipelineType, name: name, domains: domains, sender: sender });
        if (data.error) { alert('Error: ' + data.error); return; }
        document.getElementById('setup-pipeline-modal').remove();
        loadSetupPipelines();
    } catch(e) { alert('Failed: ' + e.message); }
}

async function retrySetupPipeline(id) {
    await apiPost('/api/setup-pipeline/retry', { pipeline_id: id });
    loadSetupPipelines();
}

function showSetupPipelineDetail(id) {
    fetch('/api/setup-pipeline/' + id).then(r => r.json()).then(p => {
        if (p.error) return;
        var overlay = document.createElement('div');
        overlay.className = 'pipeline-modal-overlay';
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
        var stepsHtml = p.steps.map(s => {
            var icon = stepStatusIcon(s.status);
            var color = stepStatusColor(s.status);
            var timing = s.completed_at && s.started_at
                ? Math.round((new Date(s.completed_at) - new Date(s.started_at)) / 1000) + 's'
                : s.status === 'running' ? 'running...' : '';
            var errorLine = s.error ? '<div style="color:var(--red);font-size:11px;margin-top:4px;">' + s.error + '</div>' : '';
            var retryBtn = s.status === 'failed'
                ? '<button onclick="retrySetupPipeline(\'' + p.id + '\');this.closest(\'.pipeline-modal-overlay\').remove();" style="margin-top:4px;font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;">Retry</button>'
                : '';
            return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light);">' +
                '<span style="color:' + color + ';font-size:14px;width:20px;text-align:center;">' + icon + '</span>' +
                '<span style="flex:1;font-size:13px;color:var(--text-primary);">' + s.name + '</span>' +
                '<span style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">' + s.progress + '/' + s.total + '</span>' +
                '<span style="font-size:11px;color:var(--text-muted);width:60px;text-align:right;">' + timing + '</span>' +
            '</div>' + errorLine + retryBtn;
        }).join('');
        overlay.innerHTML = '<div class="pipeline-modal">' +
            '<h3>' + p.name + ' <span style="font-size:12px;font-weight:400;color:var(--text-muted);">' + p.type + '</span></h3>' +
            '<div class="pill-stepper" style="margin-bottom:16px;">' + renderSetupPipelineSteps(p.steps) + '</div>' +
            stepsHtml +
        '</div>';
        document.body.appendChild(overlay);
    });
}

async function loadSetupPipelines() {
    try {
        var resp = await fetch('/api/setup-pipelines');
        var data = await resp.json();
        renderSetupPipelines(data.pipelines || []);
        var hasRunning = (data.pipelines || []).some(p => p.status === 'running');
        if (hasRunning && !setupPipelinePollInterval) {
            setupPipelinePollInterval = setInterval(loadSetupPipelines, 5000);
        } else if (!hasRunning && setupPipelinePollInterval) {
            clearInterval(setupPipelinePollInterval);
            setupPipelinePollInterval = null;
        }
    } catch(e) { console.error('Setup pipeline load error:', e); }
}

// --- Generic Setup Tracker ---

async function loadGenericSetupStatus() {
    try {
        var resp = await fetch('/api/generic-groups-status');
        var data = await resp.json();
        renderGenericSetupTracker(data);
    } catch (e) {
        console.error('Generic setup status error:', e);
    }
}

function renderGenericSetupTracker(data) {
    var section = document.getElementById('generic-setup-tracker');
    var content = document.getElementById('generic-setup-content');
    if (!data || (!data.running && data.step === 'unknown')) {
        section.style.display = 'none';
        if (genericTrackerInterval) { clearInterval(genericTrackerInterval); genericTrackerInterval = null; }
        return;
    }

    section.style.display = 'block';
    var completedSteps = (data.completed_steps || []).map(s => GENERIC_COMPLETED_MAP[s]).filter(Boolean);
    var currentStep = data.step || 'unknown';
    var progress = Math.round((data.progress || 0) * 100);

    var stepsHtml = GENERIC_STEP_ORDER.map(step => {
        var cls = '';
        if (completedSteps.includes(step) || (step === 'complete' && data.step === 'complete')) cls = 'done';
        else if (step === currentStep) cls = 'active';
        var icon = cls === 'done' ? '✓' : (cls === 'active' ? '●' : '○');
        return '<span class="generic-tracker-step ' + cls + '">' + icon + ' ' + GENERIC_STEP_LABELS[step] + '</span>';
    }).join('');

    var isComplete = data.step === 'complete';
    var barPct = isComplete ? 100 : progress;
    var detail = data.detail || '';
    var updatedAt = data.updated_at ? new Date(data.updated_at).toLocaleTimeString() : '';

    content.innerHTML = '<div class="generic-tracker-card">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;">' +
            '<span style="font-weight:600;font-family:var(--font-display);font-size:14px;">' +
                'Generic F / G / H / I' +
                (isComplete ? ' <span style="color:var(--accent);">✔ Complete</span>' : '') +
            '</span>' +
            '<span style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);">' +
                (updatedAt ? 'Updated ' + updatedAt : '') +
            '</span>' +
        '</div>' +
        '<div class="generic-tracker-steps">' + stepsHtml + '</div>' +
        (isComplete ? '' :
            '<div class="generic-tracker-bar"><div class="generic-tracker-bar-fill" style="width:' + barPct + '%;"></div></div>' +
            '<div class="generic-tracker-detail">' + detail + (barPct > 0 ? ' (' + barPct + '%)' : '') + '</div>'
        ) +
    '</div>';

    // Auto-poll if running
    if (data.running && !genericTrackerInterval) {
        genericTrackerInterval = setInterval(loadGenericSetupStatus, 10000);
    } else if (!data.running && genericTrackerInterval) {
        clearInterval(genericTrackerInterval);
        genericTrackerInterval = null;
    }
}

// --- Old Pipeline Tab ---

async function loadPipelines() {
    document.getElementById('pipelines-loading').style.display = 'block';
    document.getElementById('pipelines-content').innerHTML = '';
    try {
        var resp = await fetch('/api/pipeline/active');
        pipelineData = await resp.json();
        renderPipelines();
        var hasRunning = (pipelineData.pipelines || []).some(function(p) { return p.status === 'running'; });
        if (hasRunning) startPipelinePolling();
    } catch(err) {
        document.getElementById('pipelines-content').innerHTML = 'Error: ' + err.message;
    }
    document.getElementById('pipelines-loading').style.display = 'none';
}

function pipelineTypeLabel(type) {
    if (type === 'new_setup') return 'New Setup';
    if (type === 'acquisition') return 'Acquisition';
    return 'Replacement';
}

function pipelineStatusColor(status) {
    if (status === 'complete') return '#22c55e';
    if (status === 'error') return '#ef4444';
    if (status === 'awaiting_removal') return '#f59e0b';
    return '#8b5cf6';
}

function pipelineStatusLabel(status) {
    if (status === 'awaiting_removal') return 'Awaiting Removal';
    return status.charAt(0).toUpperCase() + status.slice(1);
}

function renderPipelineStepPills(p) {
    if (p.status === 'complete') return '';
    var allSteps = p.steps || [];
    var currentIdx = allSteps.indexOf(p.current_step);
    var stepSuffix = (p.retry_info && p.status === 'running')
        ? ' (attempt ' + p.retry_info.attempt + '/' + p.retry_info.max_attempts + ')'
        : '';

    var pills = allSteps.map(function(s, i) {
        var color, textColor;
        if (i < currentIdx) { color = '#22c55e'; textColor = '#fff'; }
        else if (i === currentIdx) { color = p.status === 'error' ? '#ef4444' : '#8b5cf6'; textColor = '#fff'; }
        else { color = '#333'; textColor = '#666'; }
        var label = (STEP_LABELS[s] || s) + (i === currentIdx ? stepSuffix : '');
        return '<div style="background:' + color + ';padding:4px 10px;border-radius:4px;font-size:11px;color:' + textColor + ';" title="' + label + '">' + label + '</div>';
    }).join('');

    return '<div style="display:flex;gap:4px;margin-top:12px;flex-wrap:wrap;">' + pills + '</div>';
}

function domainStatusBadge(stepStatus) {
    if (stepStatus === 'complete') return '<span style="color:var(--accent);font-weight:500;">Complete</span>';
    if (stepStatus === 'error') return '<span style="color:var(--red);font-weight:500;">Error</span>';
    if (stepStatus === 'pending') return '<span style="color:var(--text-muted);">Pending</span>';
    return '<span style="color:var(--purple);font-weight:500;">Running</span>';
}

function renderDomainDetailTable(dd) {
    var thStyle = 'text-align:left;padding:6px 8px;color:var(--text-muted);border-bottom:1px solid var(--border);';
    var html = '<div style="margin-top:12px;background:var(--bg-input);border-radius:8px;padding:12px;overflow-x:auto;">';
    html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">';
    html += '<thead><tr>';
    html += '<th style="' + thStyle + '">Domain</th>';
    html += '<th style="' + thStyle + '">Status</th>';
    html += '<th style="' + thStyle + '">Error</th>';
    html += '<th style="' + thStyle + '">Attempts</th>';
    html += '</tr></thead><tbody>';

    var tdStyle = 'padding:6px 8px;border-bottom:1px solid var(--border-light);';
    for (var domain in dd) {
        var detail = dd[domain];
        var errorText = detail.error || '—';
        var attemptText = detail.step_status === 'error'
            ? detail.attempt + '/' + detail.max_attempts + ' failed'
            : detail.step_status === 'complete' ? '—' : detail.attempt + '/' + detail.max_attempts;
        html += '<tr>';
        html += '<td style="' + tdStyle + 'color:var(--text-primary);">' + domain + '</td>';
        html += '<td style="' + tdStyle + '">' + domainStatusBadge(detail.step_status) + '</td>';
        html += '<td style="' + tdStyle + 'color:#f8a0a0;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;">' + errorText + '</td>';
        html += '<td style="' + tdStyle + 'color:var(--text-muted);">' + attemptText + '</td>';
        html += '</tr>';
    }

    html += '</tbody></table></div>';
    return html;
}

function renderPipelineErrorActions(p, dd) {
    var failedCount = 0;
    for (var dk in dd) { if (dd[dk].step_status === 'error') failedCount++; }
    return '<div style="margin-top:12px;display:flex;gap:12px;align-items:center;">' +
        '<button onclick="retryPipeline(\'' + p.id + '\')" style="background:var(--accent);color:var(--bg-root);border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;">Retry Failed (' + failedCount + ')</button>' +
        '<button onclick="skipPipelineStep(\'' + p.id + '\',\'' + p.current_step + '\')" style="background:none;color:var(--red);border:1px solid #5c1a1a;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;">Skip Step</button>' +
    '</div>';
}

function renderPendingRemovals(pendingRemovals) {
    var html = '<div style="background:var(--red-bg);border:1px solid #3d1519;border-radius:8px;padding:12px;margin-top:12px;">';
    html += '<div style="color:var(--red);font-weight:600;margin-bottom:8px;">Inboxes need removal from campaigns</div>';
    for (var email in pendingRemovals) {
        var camps = pendingRemovals[email];
        html += '<div style="margin-bottom:8px;"><div style="font-size:13px;color:#f8a0a0;">' + email + ' is in ' + camps.length + ' campaign(s):</div>';
        camps.forEach(function(c) {
            html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 4px 16px;font-size:12px;">';
            html += '<span style="color:var(--text-muted);">' + c.campaign_name + '</span>';
            html += '<button onclick="removeFromCampaign(\'' + email + '\',' + c.campaign_id + ')" style="background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:11px;">Remove</button></div>';
        });
        html += '<button onclick="removeFromAllCampaigns(\'' + email + '\')" style="background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:4px;">Remove from all campaigns</button>';
        html += '</div>';
    }
    html += '</div>';
    return html;
}

function renderPipelineCard(p) {
    var html = '<div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:12px;">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">';
    html += '<div><span style="font-size:16px;font-weight:600;">' + p.client_name + '</span>';
    html += '<span style="font-size:13px;color:var(--text-muted);margin-left:12px;">' + pipelineTypeLabel(p.type) + '</span></div>';
    html += '<span style="color:' + pipelineStatusColor(p.status) + ';font-weight:500;">' + pipelineStatusLabel(p.status) + '</span></div>';
    html += '<div style="font-size:13px;color:var(--text-muted);margin-bottom:8px;">Domains: ' + p.domains.length + '</div>';
    html += '<div style="font-size:12px;color:var(--text-muted);">Started: ' + new Date(p.created_at).toLocaleString() + '</div>';

    html += renderPipelineStepPills(p);

    var dd = p.domain_details || {};
    var hasErrors = false;
    for (var dk in dd) { if (dd[dk].step_status === 'error') { hasErrors = true; break; } }
    if ((p.status === 'error' || hasErrors) && Object.keys(dd).length > 0) {
        html += renderDomainDetailTable(dd);
    }

    if (p.status === 'error') {
        html += renderPipelineErrorActions(p, dd);
    }

    if (p.status === 'awaiting_removal' && p.pending_removals) {
        html += renderPendingRemovals(p.pending_removals);
    }

    var isGeneric = p.client_name && p.client_name.toLowerCase().indexOf('generic') === 0;
    if (isGeneric && (p.status === 'complete' || p.status === 'running')) {
        html += '<div style="margin-top:12px;display:flex;justify-content:flex-end;">';
        html += '<button onclick="event.stopPropagation();openAssignModal(\'' + p.id + '\',\'' + p.client_name.replace(/'/g, "\\'") + '\')" style="background:var(--purple);color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:500;font-size:13px;">Assign to Client</button>';
        html += '</div>';
    }

    if (p.errors && p.errors.length > 0) {
        html += '<div style="margin-top:8px;font-size:12px;color:var(--red);">';
        p.errors.forEach(function(e) { html += '<div>' + e + '</div>'; });
        html += '</div>';
    }

    html += '</div>';
    return html;
}

function renderPipelines() {
    var pipelines = pipelineData.pipelines || [];

    var active = pipelines.filter(p => p.status === 'running' || p.status === 'awaiting_removal');
    var badge = document.getElementById('pipeline-badge');
    if (active.length > 0) {
        badge.style.display = 'inline';
        badge.textContent = active.length + ' active';
    } else {
        badge.style.display = 'none';
    }

    if (pipelines.length === 0) {
        document.getElementById('pipelines-content').innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:40px;">No pipelines yet. Start one from the SmartLead tab.</div>';
        return;
    }

    document.getElementById('pipelines-content').innerHTML = pipelines.map(renderPipelineCard).join('');
}

async function removeFromCampaign(email, campaignId) {
    if (!confirm('Remove ' + email + ' from this campaign?')) return;
    try {
        await apiPost('/api/inbox/remove-from-campaign', {email: email, campaign_id: campaignId});
        loadPipelines();
    } catch(e) { alert('Error: ' + e.message); }
}

async function removeFromAllCampaigns(email) {
    if (!confirm('Remove ' + email + ' from ALL active campaigns?')) return;
    try {
        await apiPost('/api/inbox/remove-from-all-campaigns', {email: email});
        loadPipelines();
    } catch(e) { alert('Error: ' + e.message); }
}

async function retryPipeline(pipelineId, domains) {
    try {
        var result = await apiPost('/api/pipeline/retry', {pipeline_id: pipelineId, domains: domains || []});
        if (result.error) {
            alert('Retry failed: ' + result.error);
        } else {
            loadPipelines();
            startPipelinePolling();
        }
    } catch(e) { alert('Error: ' + e.message); }
}

async function skipPipelineStep(pipelineId, stepName) {
    var label = STEP_LABELS[stepName] || stepName;
    if (!confirm('Skip "' + label + '"? Domains that failed this step may have incomplete setup. This should only be used as a last resort.')) return;
    try {
        var result = await apiPost('/api/pipeline/skip-step', {pipeline_id: pipelineId});
        if (result.error) {
            alert('Skip failed: ' + result.error);
        } else {
            alert('Skipped ' + label + '. Pipeline moving to: ' + (STEP_LABELS[result.next_step] || result.next_step));
            loadPipelines();
            startPipelinePolling();
        }
    } catch(e) { alert('Error: ' + e.message); }
}

function startPipelinePolling() {
    if (pipelinePollingInterval) return;
    pipelinePollingInterval = setInterval(async function() {
        var pipelines = (pipelineData || {}).pipelines || [];
        var hasRunning = pipelines.some(function(p) { return p.status === 'running'; });
        if (!hasRunning) {
            stopPipelinePolling();
            return;
        }
        try {
            var resp = await fetch('/api/pipeline/active');
            pipelineData = await resp.json();
            renderPipelines();
        } catch(e) { /* silent */ }
    }, 10000);
}

function stopPipelinePolling() {
    if (pipelinePollingInterval) {
        clearInterval(pipelinePollingInterval);
        pipelinePollingInterval = null;
    }
}
