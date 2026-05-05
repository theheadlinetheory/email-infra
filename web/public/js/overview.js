function safeParseJson(resp) {
    if (!resp || !resp.ok) return Promise.resolve(null);
    return resp.json().catch(function() { return null; });
}

function rateColor(value, thresholds) {
    if (value === null || value === undefined) return 'var(--text-muted)';
    if (thresholds.ascending) {
        if (value > thresholds.good) return 'var(--accent)';
        if (value > thresholds.warn) return '#f59e0b';
        return '#ef4444';
    }
    if (value > thresholds.bad) return '#ef4444';
    if (value > thresholds.warn) return '#f59e0b';
    return 'var(--accent)';
}

function rateDisplay(value) {
    return (value !== null && value !== undefined) ? value + '%' : '—';
}

function computeAverageRate(items, field) {
    var values = items.filter(function(c) { return c[field] !== null && c[field] !== undefined && c[field] > 0; }).map(function(c) { return c[field]; });
    return values.length ? (values.reduce(function(a, b) { return a + b; }, 0) / values.length).toFixed(1) : '—';
}

function rateCssClass(value, thresholds) {
    if (value === '—') return 'good';
    var n = parseFloat(value);
    if (thresholds.ascending) {
        if (n > thresholds.good) return 'good';
        if (n > thresholds.warn) return 'warn';
        return 'alert';
    }
    if (n > thresholds.bad) return 'alert';
    if (n > thresholds.warn) return 'warn';
    return 'good';
}

async function loadOverview() {
    document.getElementById('loading').style.display = 'block';
    document.getElementById('content').style.display = 'none';

    try {
        var [overviewResp, unassignedResp, genericResp, acquisitionResp, inventoryResp, rotationResp, setupPipelineResp, genericSetupResp, untaggedResp] = await Promise.all([
            fetch('/api/overview'),
            fetch('/api/unassigned').catch(function() { return null; }),
            fetch('/api/generic-groups').catch(function() { return null; }),
            fetch('/api/acquisition').catch(function() { return null; }),
            fetch('/api/domain-inventory').catch(function() { return null; }),
            fetch('/api/rotation/status').catch(function() { return null; }),
            fetch('/api/setup-pipelines').catch(function() { return null; }),
            fetch('/api/generic-groups-status').catch(function() { return null; }),
            fetch('/api/untagged-count').catch(function() { return null; }),
        ]);

        var text = await overviewResp.text();
        try {
            overviewData = JSON.parse(text);
        } catch (parseErr) {
            document.getElementById('loading').innerHTML = 'Error parsing response: ' + text.substring(0, 500);
            return;
        }
        if (overviewData.error) {
            document.getElementById('loading').innerHTML = '<div style="text-align:left;max-width:800px;margin:0 auto;"><h3 style="color:var(--red);">API Error</h3><pre style="white-space:pre-wrap;color:#f8a0a0;font-size:12px;">' + overviewData.traceback + '</pre></div>';
            return;
        }

        var unassignedData = await safeParseJson(unassignedResp);
        var genericData = await safeParseJson(genericResp);
        var acquisitionData = await safeParseJson(acquisitionResp);
        acquisitionDataGlobal = acquisitionData;
        inventoryData = await safeParseJson(inventoryResp);
        var rotationData = await safeParseJson(rotationResp);
        var untaggedData = await safeParseJson(untaggedResp);

        renderUntaggedAlert(untaggedData);
        renderOverview();
        renderUnassignedSection(unassignedData);
        renderGenericSection(genericData);
        renderAcquisitionSection(acquisitionData);
        renderSetupPipelineSection(await safeParseJson(setupPipelineResp));
        renderRotation(rotationData);

        var genericSetupData = await safeParseJson(genericSetupResp);
        if (genericSetupData) renderGenericSetupTracker(genericSetupData);
    } catch (err) {
        document.getElementById('loading').innerHTML = 'Error loading data: ' + err.message;
    }
}

function renderUntaggedAlert(untaggedData) {
    var untaggedAlert = document.getElementById('untagged-alert');
    if (currentMode === 'fulfillment' && untaggedData && untaggedData.untagged_count > 0) {
        document.getElementById('untagged-alert-text').textContent =
            '⚠ ' + untaggedData.untagged_count + ' accounts have no client assignment and may be missing tags. Run fix_untagged.py to remediate.';
        untaggedAlert.style.display = 'flex';
    } else if (untaggedAlert) {
        untaggedAlert.style.display = 'none';
    }
}

function renderUnassignedSection(unassignedData) {
    if (unassignedData && unassignedData.count > 0) {
        document.getElementById('unassigned-section').style.display = 'block';
        renderUnassigned(unassignedData.accounts);
    } else {
        document.getElementById('unassigned-section').style.display = 'none';
    }
}

function renderGenericSection(genericData) {
    if (!genericData || !genericData.groups || genericData.groups.length === 0) {
        document.getElementById('generic-section').style.display = 'none';
        return;
    }
    document.getElementById('generic-section').style.display = 'block';
    var ready = genericData.groups.filter(function(g) { return g.status === 'ready'; }).length;
    var warming = genericData.groups.filter(function(g) { return g.status === 'warming'; }).length;
    var html = statCard(genericData.total_accounts, 'Generic Inboxes');
    html += statCard(ready, 'Ready for Clients', 'good');
    if (warming > 0) html += statCard(warming, 'Still Warming', 'warn');
    html += statCard(genericData.total_daily_capacity + '/day', 'Total Capacity');
    document.getElementById('generic-stats').innerHTML = html;
    renderGenericGroups(genericData.groups);
}

function renderAcquisitionSection(acquisitionData) {
    if (!acquisitionData || !acquisitionData.total_groups) {
        document.getElementById('acquisition-section').style.display = 'none';
        return;
    }
    document.getElementById('acquisition-section').style.display = currentMode === 'acquisition' ? 'block' : 'none';
    var html = statCard(acquisitionData.total_accounts, 'Acquisition Inboxes');
    html += statCard(acquisitionData.total_groups, 'Active Groups');
    document.getElementById('acquisition-stats').innerHTML = html;
    renderAcqAlerts(acquisitionData);
    renderAcquisitionGroups(acquisitionData.groups);
}

function renderSetupPipelineSection(setupPipelineData) {
    if (!setupPipelineData || !setupPipelineData.pipelines || setupPipelineData.pipelines.length === 0) return;
    renderSetupPipelines(setupPipelineData.pipelines);
    var hasRunning = setupPipelineData.pipelines.some(function(p) { return p.status === 'running'; });
    if (hasRunning && !setupPipelinePollInterval) {
        setupPipelinePollInterval = setInterval(loadSetupPipelines, 5000);
    }
}

function renderCardHTML(item) {
    var issues = (item.smtp_failures || 0) + (item.blocked || 0);
    var issuesColor = issues > 0 ? '#ef4444' : 'var(--accent)';
    var bounceVal = rateDisplay(item.avg_bounce_rate);
    var bounceColor = rateColor(item.avg_bounce_rate, {bad: 3, warn: 1});
    var replyVal = rateDisplay(item.avg_reply_rate);
    var replyColor = rateColor(item.avg_reply_rate, {ascending: true, good: 5, warn: 2});

    var html = `<div class="client-card ${item.needs_attention ? 'has-alert' : ''}" onclick="openDetail(${item.id}, '${(item.name || '').replace(/'/g, "\\'")}')">`;
    // Header — name + count + warmup badge
    var warmupBadge = '';
    if (item.still_warming && item.warmup_done_date) {
        warmupBadge = `<span class="badge badge-yellow" style="font-size:10px;margin-left:6px;">Ready ${item.warmup_done_date}</span>`;
    }
    html += `<div class="cc-header"><span class="cc-name">${item.name}</span><span class="cc-count">${item.accounts} accounts${warmupBadge}</span></div>`;
    // Alert banner
    if (item.needs_attention) {
        html += `<div style="background:var(--red-bg);border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:12px;color:var(--red);">${item.flagged_domains}/${item.total_domains} domains flagged (${item.flagged_pct}%)</div>`;
    }
    // Stats: Capacity, Issues, Bounce, Reply
    var capacityDisplay = (item.daily_capacity || 0) + '/day';
    if (item.still_warming && item.daily_capacity < item.projected_capacity) {
        capacityDisplay = (item.daily_capacity || 0) + ' → ' + item.projected_capacity + '/day';
    }
    html += `<div class="cc-stats">`;
    html += `<div class="cc-stat"><span class="label">Capacity</span><span>${capacityDisplay}</span></div>`;
    html += `<div class="cc-stat"><span class="label">Issues</span><span style="color:${issuesColor}">${issues}</span></div>`;
    html += `<div class="cc-stat"><span class="label">Bounce Rate</span><span style="color:${bounceColor}">${bounceVal}</span></div>`;
    html += `<div class="cc-stat"><span class="label">Reply Rate</span><span style="color:${replyColor}">${replyVal}</span></div>`;
    html += `</div>`;
    // Batch warmup bars
    if (item.batches && item.batches.length > 0) {
        var warmingBatches = item.batches.filter(b => b.status === 'warming');
        var readyBatches = item.batches.filter(b => b.status === 'ready');
        if (warmingBatches.length > 0 || readyBatches.length > 1) {
            html += `<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;">`;
            for (var b of item.batches) {
                if (b.status === 'ready') {
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:4px;"><span style="color:var(--accent);">&#9679; ${b.total} accounts ready</span><span style="color:var(--text-muted);">since ${b.warmup_start}</span></div>`;
                } else {
                    var pct = Math.round(b.days_done / 14 * 100);
                    html += `<div style="margin-bottom:6px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:3px;"><span style="color:var(--purple);">&#9679; ${b.total} new accounts warming</span><span>Day ${b.days_done}/14</span></div><div style="background:var(--bg-input);border-radius:4px;height:5px;overflow:hidden;"><div style="background:var(--purple);height:100%;width:${pct}%;border-radius:4px;"></div></div></div>`;
                }
            }
            html += `</div>`;
        }
    }
    // Campaign assignment (acquisition groups)
    if (item.active_campaigns || item.paused_campaigns) {
        var active = item.active_campaigns || [];
        var paused = item.paused_campaigns || [];
        var currentCampId = active.length === 1 ? active[0].id : '';
        html += `<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;font-size:12px;">`;
        if (item.campaign_conflict) {
            html += `<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:6px 10px;margin-bottom:6px;color:#dc2626;font-weight:600;">CONFLICT: ${active.length} active campaigns</div>`;
            active.forEach(c => {
                html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;"><span style="color:#dc2626;">&#9679; ${c.name}</span><button onclick="event.stopPropagation();unassignGroupCampaign(${item.id},'${item.name.replace(/'/g, "\\'")}',${c.id},'${c.name.replace(/'/g, "\\'")}')" style="font-size:10px;padding:2px 8px;border:1px solid #fecaca;border-radius:4px;background:#fef2f2;color:#dc2626;cursor:pointer;">Remove</button></div>`;
            });
        } else {
            html += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">`;
            html += `<span class="label" style="white-space:nowrap;">Campaign</span>`;
            html += `<select onchange="event.stopPropagation();assignGroupCampaign(${item.id},'${item.name.replace(/'/g, "\\'")}',this.value,${currentCampId || 0})" style="flex:1;font-size:12px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-input);color:var(--text-primary);max-width:200px;cursor:pointer;" data-group-id="${item.id}">`;
            html += `<option value="">— Available —</option>`;
            html += `</select></div>`;
        }
        if (paused.length > 0) {
            html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;"><span class="label">Paused</span><span style="color:var(--text-muted);">${paused.map(c => c.name).join(', ')}</span></div>`;
        }
        html += `</div>`;
    }
    // Footer dates
    var hasReady = item.ready_date && item.days_until_ready !== null && item.days_until_ready > 0;
    var hasRotation = !!item.rotation_date;
    if (hasReady || hasRotation) {
        html += `<div class="cc-dates">`;
        if (hasReady) {
            var readyBadge = item.days_until_ready <= 0 ? '<span class="badge badge-green">Ready</span>' : '<span class="badge badge-yellow">' + item.days_until_ready + 'd left</span>';
            html += `<div class="date-row"><span>Ready Date</span><span>${item.ready_date} ${readyBadge}</span></div>`;
        }
        if (hasRotation) {
            var rotBadge = item.days_until_rotation !== null && item.days_until_rotation <= 7 ? ' <span class="badge badge-red">Rotate soon</span>' : '';
            html += `<div class="date-row"><span>Rotation Date</span><span>${item.rotation_date}${rotBadge}</span></div>`;
        }
        html += `</div>`;
    }
    html += `</div>`;
    return html;
}

function renderClientCards(clients) {
    return clients.map(function(cl) { return renderCardHTML(cl); }).join('');
}

function renderAlertBanner(d) {
    var alertEl = document.getElementById('alert-banner');
    var attentionClients = d.clients.filter(function(c) { return c.needs_attention; });
    var invLow = inventoryData && (inventoryData.client_low || inventoryData.acquisition_low);
    var hasAlerts = d.blocked_accounts.length > 0 || d.smtp_failures > 0 || attentionClients.length > 0 || d.idle_inboxes > 0 || invLow;

    if (currentMode !== 'fulfillment' || !hasAlerts) {
        alertEl.innerHTML = '';
        return;
    }

    var html = '<div class="alert-banner"><h3>Alerts</h3>';
    if (inventoryData && inventoryData.client_low) {
        html += '<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Client pool has ' + inventoryData.client_available + ' available (need 20+)</div>';
    }
    if (inventoryData && inventoryData.acquisition_low) {
        html += '<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Acquisition pool has ' + inventoryData.acquisition_available + ' available (need 20+)</div>';
    }
    if (d.idle_inboxes > 0) {
        html += '<div class="alert-item" style="font-size:14px;margin-bottom:6px;color:var(--yellow);">' + d.idle_inboxes + ' warmed inbox(es) across ' + d.idle_clients + ' client(s) are not in any campaign</div>';
    }
    if (attentionClients.length > 0) {
        html += '<div class="alert-item" style="font-size:14px;margin-bottom:6px;">' + attentionClients.length + ' client(s) have infrastructure that needs attention</div>';
        attentionClients.forEach(function(c) {
            html += '<div class="alert-item" style="padding-left:16px;">' + c.name + ' — ' + c.flagged_domains + '/' + c.total_domains + ' domains flagged (health score: ' + c.health_score + ')</div>';
        });
    }
    if (d.smtp_failures > 0) html += '<div class="alert-item">' + d.smtp_failures + ' accounts with SMTP failures</div>';
    if (d.imap_failures > 0) html += '<div class="alert-item">' + d.imap_failures + ' accounts with IMAP failures</div>';
    var grouped = {};
    d.blocked_accounts.forEach(function(b) {
        var short = b.reason.split(':')[0] || 'Unknown';
        if (!grouped[short]) grouped[short] = [];
        grouped[short].push(b.email.split('@')[1]);
    });
    for (var [reason, domains] of Object.entries(grouped)) {
        var unique = [...new Set(domains)];
        html += '<div class="alert-item">' + unique.length + ' domain(s) blocked — ' + reason + ': ' + unique.join(', ') + '</div>';
    }
    html += '</div>';
    alertEl.innerHTML = html;
}

function renderOverview() {
    document.getElementById('loading').style.display = 'none';
    var content = document.getElementById('content');
    content.style.display = 'block';
    content.style.animation = 'fadeIn 0.25s ease-out';

    var d = overviewData;
    var time = new Date(d.generated_at).toLocaleTimeString();
    document.getElementById('last-updated').textContent = 'Updated: ' + time;

    // Inventory badges
    if (inventoryData) {
        document.getElementById('inventory-badges').style.display = 'flex';
        var clientBadge = document.getElementById('inv-client');
        var acqBadge = document.getElementById('inv-acq');
        clientBadge.textContent = 'Client: ' + inventoryData.client_available;
        clientBadge.className = 'badge ' + (inventoryData.client_low ? 'badge-red' : 'badge-green');
        acqBadge.textContent = 'Acq: ' + inventoryData.acquisition_available;
        acqBadge.className = 'badge ' + (inventoryData.acquisition_low ? 'badge-red' : 'badge-green');
    }

    renderAlertBanner(d);

    // Subtitle + filter bar
    var archivedClients = d.archived_clients || [];
    var activeClients = d.clients.filter(cl => !archivedClients.includes(cl.name));
    var archivedClientData = d.clients.filter(cl => archivedClients.includes(cl.name));
    var activeCount = activeClients.length;
    var archivedCount = archivedClientData.length;

    // Store for filtering
    window._activeClients = activeClients;
    window._archivedClientData = archivedClientData;
    window._archivedClients = archivedClients;
    window._pausedClients = d.paused_clients || [];

    document.getElementById('sl-subtitle').textContent = `${activeCount} active clients, ${d.total_accounts} accounts, ${d.in_campaign} in campaigns`;

    var filterBar = document.getElementById('sl-filter-bar');
    filterBar.innerHTML = `
        <button class="filter-pill active" onclick="filterClients('active')">Active <span class="count">${activeCount}</span></button>
        <button class="filter-pill" onclick="filterClients('archived')">Archived <span class="count">${archivedCount}</span></button>
    `;

    // Client cards
    var gridEl = document.getElementById('clients-grid');
    clientsList = activeClients;
    gridEl.innerHTML = renderClientCards(activeClients);

    // Populate assign dropdown
    var select = document.getElementById('assign-client-select');
    var currentVal = select.value;
    select.innerHTML = '<option value="">-- Assign to client --</option>';
    d.clients.forEach(cl => {
        select.innerHTML += `<option value="${cl.id}">${cl.name}</option>`;
    });
    select.value = currentVal;

    applyModeVisibility(currentMode);
}

function renderUnassigned(accounts) {
    var tbody = document.getElementById('unassigned-body');
    tbody.innerHTML = accounts.map(a => `
        <tr>
            <td><input type="checkbox" class="ua-check" value="${a.id}" onchange="updateAssignBtn()"></td>
            <td>${a.email}</td>
            <td>${a.domain}</td>
            <td style="color:${a.warmup_status === 'ACTIVE' ? '#22c55e' : '#f59e0b'}">${a.warmup_status}</td>
            <td>${a.warmup_reputation}</td>
            <td style="color:${a.smtp_ok ? '#22c55e' : '#ef4444'}">${a.smtp_ok ? 'OK' : 'FAIL'}</td>
        </tr>
    `).join('');
}

function toggleSelectAll() {
    var checked = document.getElementById('select-all').checked;
    document.querySelectorAll('.ua-check').forEach(cb => cb.checked = checked);
    updateAssignBtn();
}

function updateAssignBtn() {
    var selected = document.querySelectorAll('.ua-check:checked').length;
    var clientSelected = document.getElementById('assign-client-select').value;
    document.getElementById('assign-btn').disabled = !(selected > 0 && clientSelected);
}

async function assignSelected() {
    var accountIds = Array.from(document.querySelectorAll('.ua-check:checked')).map(cb => parseInt(cb.value));
    var clientId = parseInt(document.getElementById('assign-client-select').value);
    if (!accountIds.length || !clientId) return;

    document.getElementById('assign-btn').disabled = true;
    document.getElementById('assign-status').textContent = 'Assigning ' + accountIds.length + ' accounts...';

    try {
        var result = await apiPost('/api/assign', {account_ids: accountIds, client_id: clientId});
        document.getElementById('assign-status').textContent =
            'Done! ' + result.success + ' assigned, ' + result.fail + ' failed.';
        setTimeout(() => loadOverview(), 2000);
    } catch (err) {
        document.getElementById('assign-status').textContent = 'Error: ' + err.message;
    }
}

function renderGenericGroups(groups) {
    var grid = document.getElementById('generic-grid');
    grid.innerHTML = groups.map(g => {
        var isReady = g.status === 'ready';
        var statusColor = isReady ? '#22c55e' : '#8b5cf6';
        var statusBg = isReady ? '#f0fdf4' : '#f5f3ff';
        var statusLabel = isReady ? 'Ready' : g.days_left + 'd left';
        var progressPct = isReady ? 100 : Math.min(100, Math.round((g.days_warming / 14) * 100));

        var html = `
        <div class="client-card" style="position:relative;cursor:pointer;" onclick="openDetail(${g.client_id}, '${g.name.replace(/'/g, "\\'")}')">
            <div class="cc-header">
                <span class="cc-name">${g.name}</span>
                <div style="display:flex;align-items:center;gap:8px;">
                    <span class="badge" style="background:${statusBg};color:${statusColor};font-size:13px;padding:3px 10px;">${statusLabel}</span>
                    <span class="cc-count">${g.accounts} accounts</span>
                </div>
            </div>
            <div class="cc-stats">
                <div class="cc-stat"><span class="label">Domains</span><span>${g.domains}</span></div>
                <div class="cc-stat"><span class="label">Capacity</span><span>${g.daily_capacity}/day</span></div>
                <div class="cc-stat"><span class="label">Warmup Start</span><span>${g.warmup_start || '—'}</span></div>
                <div class="cc-stat"><span class="label">${isReady ? 'Ready Since' : 'Ready Date'}</span><span>${g.ready_date || '—'}</span></div>
                <div class="cc-stat"><span class="label">Health</span><span style="color:${g.health_score >= 85 ? '#22c55e' : g.health_score >= 60 ? '#f59e0b' : '#ef4444'}">${g.health_score}</span></div>
                <div class="cc-stat"><span class="label">SMTP Fail</span><span style="color:${g.smtp_failures > 0 ? '#ef4444' : '#22c55e'}">${g.smtp_failures}</span></div>
            </div>
            <div style="margin-top:10px;">
                <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:4px;">
                    <span>Warmup Progress</span><span>${progressPct}%</span>
                </div>
                <div style="background:var(--bg-input);border-radius:4px;height:6px;overflow:hidden;">
                    <div style="background:${isReady ? '#22c55e' : '#8b5cf6'};height:100%;width:${progressPct}%;border-radius:4px;transition:width 0.3s;"></div>
                </div>
            </div>
            <div style="margin-top:12px;display:flex;justify-content:flex-end;">
                <button onclick="event.stopPropagation();openAssignModal('${g.pipeline_id || ''}','${g.name.replace(/'/g, "\\'")}')" style="background:${isReady ? 'var(--purple)' : 'var(--bg-raised)'};color:${isReady ? '#fff' : 'var(--text-secondary)'};border:1px solid ${isReady ? 'var(--purple)' : 'var(--border)'};padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:500;font-size:13px;">Assign to Client</button>
            </div>
        </div>`;
        return html;
    }).join('');
}

async function loadAcquisition() {
    try {
        var resp = await fetch('/api/acquisition');
        var data = await resp.json();
        acquisitionDataGlobal = data;
        if (data && data.groups) {
            document.getElementById('acquisition-stats').innerHTML =
                statCard(data.total_accounts, 'Acquisition Inboxes') +
                statCard(data.total_groups, 'Active Groups');
            renderAcqAlerts(data);
            renderAcquisitionGroups(data.groups);
        }
    } catch (e) {
        console.error('loadAcquisition error:', e);
    }
}

function renderAcqAlerts(data) {
    var el = document.getElementById('acq-conflict-banner');
    if (!el) return;
    var conflicts = data.campaign_conflicts || [];
    var empty = data.empty_campaigns || [];
    if (conflicts.length === 0 && empty.length === 0) {
        el.innerHTML = '';
        return;
    }
    var html = '';
    if (conflicts.length > 0) {
        html += '<div class="alert-banner" style="border-color:#fecaca;"><h3 style="color:#dc2626;">Campaign Conflicts</h3>';
        conflicts.forEach(c => {
            html += '<div class="alert-item" style="color:#dc2626;">' + c.group + ' is in ' + c.campaigns.length + ' active campaigns: ' + c.campaigns.join(', ') + '</div>';
        });
        html += '</div>';
    }
    if (empty.length > 0) {
        html += '<div class="alert-banner" style="border-color:#fed7aa;"><h3 style="color:#ea580c;">Campaigns With No Inboxes</h3>';
        empty.forEach(c => {
            html += '<div class="alert-item" style="color:#ea580c;">' + c.name + ' — active but has no email accounts assigned</div>';
        });
        html += '</div>';
    }
    el.innerHTML = html;
}

function renderAcqUnassigned(accounts) {
    var section = document.getElementById('acq-unassigned-section');
    if (!accounts || accounts.length === 0) {
        section.style.display = 'none';
        return;
    }
    section.style.display = 'block';
    document.getElementById('acq-unassigned-count').textContent = accounts.length + ' inbox(es) with Headline Theory domains not assigned to any acquisition group';
    var tbody = document.getElementById('acq-unassigned-body');
    tbody.innerHTML = accounts.map(a => `
        <tr>
            <td>${a.email}</td>
            <td>${a.from_name || '—'}</td>
            <td>${a.domain}</td>
            <td style="color:${a.warmup_status === 'ACTIVE' ? '#22c55e' : '#f59e0b'}">${a.warmup_status}</td>
            <td>${a.warmup_reputation}</td>
            <td style="color:${a.smtp_ok ? '#22c55e' : '#ef4444'}">${a.smtp_ok ? 'OK' : 'FAIL'}</td>
        </tr>
    `).join('');
}

function renderAcquisitionGroups(groups) {
    var grid = document.getElementById('acquisition-grid');
    grid.innerHTML = groups.map(g => renderCardHTML(g)).join('');
    // Populate campaign dropdowns after rendering
    populateCampaignDropdowns(groups);
}

async function populateCampaignDropdowns(groups) {
    // Fetch acquisition campaigns (cached in-memory for the session)
    if (!acqCampaignsCache) {
        try {
            var resp = await fetch('/api/acquisition-campaigns');
            var data = await resp.json();
            acqCampaignsCache = data.campaigns || [];
        } catch (e) {
            console.error('Failed to load acquisition campaigns:', e);
            return;
        }
    }
    // Populate each group's dropdown
    var selects = document.querySelectorAll('select[data-group-id]');
    selects.forEach(select => {
        var groupId = parseInt(select.dataset.groupId);
        var group = groups.find(g => g.id === groupId);
        var activeCampId = group && group.active_campaigns && group.active_campaigns.length === 1 ? group.active_campaigns[0].id : null;

        acqCampaignsCache.forEach(c => {
            var opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.name + (c.status === 'PAUSED' ? ' (paused)' : '');
            if (c.id === activeCampId) opt.selected = true;
            select.appendChild(opt);
        });
    });
}

async function assignGroupCampaign(groupClientId, groupName, newCampId, currentCampId) {
    // Unassign from current campaign first if changing
    if (currentCampId && currentCampId !== parseInt(newCampId)) {
        if (!confirm(`Remove ${groupName} from current campaign before assigning to new one?`)) return;
        try {
            await apiPost('/api/acquisition/assign-campaign', {group_client_id: groupClientId, group_name: groupName, campaign_id: currentCampId, action: 'unassign'});
        } catch (e) {
            alert('Failed to unassign: ' + e.message);
            return;
        }
    }
    // Assign to new campaign
    if (newCampId) {
        var campName = acqCampaignsCache ? (acqCampaignsCache.find(c => c.id === parseInt(newCampId)) || {}).name || '' : '';
        if (!confirm(`Assign all ${groupName} accounts to "${campName}"?`)) {
            loadAcquisition();
            return;
        }
        try {
            var result = await apiPost('/api/acquisition/assign-campaign', {group_client_id: groupClientId, group_name: groupName, campaign_id: parseInt(newCampId), action: 'assign'});
            if (result.error) {
                alert('Error: ' + result.error);
            }
        } catch (e) {
            alert('Failed to assign: ' + e.message);
        }
    }
    acqCampaignsCache = null; // Clear cache to refresh
    loadAcquisition();
}

async function unassignGroupCampaign(groupClientId, groupName, campId, campName) {
    if (!confirm(`Remove ${groupName} from "${campName}"?`)) return;
    try {
        var result = await apiPost('/api/acquisition/assign-campaign', {group_client_id: groupClientId, group_name: groupName, campaign_id: campId, action: 'unassign'});
        if (result.error) {
            alert('Error: ' + result.error);
        }
    } catch (e) {
        alert('Failed to unassign: ' + e.message);
    }
    acqCampaignsCache = null;
    loadAcquisition();
}

function renderRotation(data) {
    var grid = document.getElementById('rotation-grid');
    var section = document.getElementById('rotation-section');
    if (!data || !data.rotations || data.rotations.length === 0) {
        section.style.display = 'none';
        return;
    }
    section.style.display = 'block';
    var html = '';
    for (var rot of data.rotations) {
        var aCount = (rot.group_a_ids || []).length;
        var bCount = (rot.group_b_ids || []).length;
        var active = rot.active_group || 'A';
        var lastSwap = rot.last_swap_date || 'Never';
        var aBadge = active === 'A' ? 'badge-green' : 'badge-muted';
        var bBadge = active === 'B' ? 'badge-green' : 'badge-muted';
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

function applyModeVisibility(mode) {
    // 1. Button active states
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.mode-btn[onclick="switchMode('${mode}')"]`).classList.add('active');

    // 2. Show/hide fulfillment vs acquisition sections
    var isFulfillment = mode === 'fulfillment';
    var fulfillmentSections = ['clients-grid', 'generic-section', 'rotation-section', 'unassigned-section', 'generic-setup-tracker'];
    var acquisitionSections = ['acquisition-section'];
    fulfillmentSections.forEach(function(id) { var el = document.getElementById(id); if (el) el.style.display = isFulfillment ? '' : 'none'; });
    acquisitionSections.forEach(function(id) { var el = document.getElementById(id); if (el) el.style.display = isFulfillment ? 'none' : ''; });

    // 3. Render scoped summary stats
    if (!overviewData) return;
    var d = overviewData;
    var summaryEl = document.getElementById('summary-row');

    var items, countLabel1, countVal1, countLabel2, countVal2;
    if (mode === 'fulfillment') {
        items = d.clients;
        countVal1 = d.total_accounts;
        countLabel1 = 'Total Accounts';
        countVal2 = d.in_campaign;
        countLabel2 = 'In Campaigns';
    } else {
        var aq = acquisitionDataGlobal;
        if (!aq) return;
        items = aq.groups;
        countVal1 = aq.total_accounts;
        countLabel1 = 'Total Accounts';
        countVal2 = aq.total_groups;
        countLabel2 = 'Active Groups';
    }

    var avgBounce = computeAverageRate(items, 'avg_bounce_rate');
    var avgReply = computeAverageRate(items, 'avg_reply_rate');
    var bounceClass = rateCssClass(avgBounce, {bad: 3, warn: 1});
    var replyClass = rateCssClass(avgReply, {ascending: true, good: 5, warn: 2});
    var bounceSuffix = avgBounce !== '—' ? '%' : '';
    var replySuffix = avgReply !== '—' ? '%' : '';

    summaryEl.innerHTML = statCard(countVal1, countLabel1, 'good')
        + statCard(countVal2, countLabel2, 'good')
        + statCard(avgBounce + bounceSuffix, 'Avg Bounce Rate', bounceClass)
        + statCard(avgReply + replySuffix, 'Avg Reply Rate', replyClass);
}

function filterClients(filter) {
    document.querySelectorAll('.filter-pill').forEach(function(p) { p.classList.remove('active'); });
    event.target.closest('.filter-pill').classList.add('active');

    var clients = filter === 'archived' ? (window._archivedClientData || []) : (window._activeClients || []);
    clientsList = clients;
    document.getElementById('clients-grid').innerHTML = renderClientCards(clients);
}

document.getElementById('assign-client-select').addEventListener('change', updateAssignBtn);
