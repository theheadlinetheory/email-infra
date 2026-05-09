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

// ── Secondary Tabs (ZapMail, Domains, Sync, Wallet) ──

// --- ZapMail ---

async function loadZapmail() {
    document.getElementById('zm-clients').innerHTML = '<div class="loading"><span class="spinner"></span> Loading ZapMail data...</div>';
    try {
        var resp = await fetch('/api/zapmail');
        zmData = await resp.json();
        renderZapmail();
    } catch (err) {
        document.getElementById('zm-clients').innerHTML = 'Error: ' + err.message;
    }
}

function renderZapmail() {
    var d = zmData;

    // Summary
    var renewingSoon = d.clients.reduce(function(n, c) { return n + c.renewing_soon; }, 0);
    document.getElementById('zm-summary-row').innerHTML =
        statCard(d.total_domains, 'Total Domains', 'good') +
        statCard(d.total_mailboxes, 'Total Mailboxes', 'good') +
        statCard(d.clients.length, 'Client Tags', 'good') +
        statCard(renewingSoon, 'Renewing in 3 days', renewingSoon > 0 ? 'alert' : 'good');

    // Client cards with domain tables
    document.getElementById('zm-clients').innerHTML = d.clients.map(cl => {
        var cardId = 'zm-' + cl.name.replace(/[^a-zA-Z0-9]/g, '_');
        return `
        <div class="zm-client-card ${cl.renewing_soon > 0 ? 'has-renewal' : ''}">
            <div class="zm-client-header" onclick="document.getElementById('${cardId}').style.display = document.getElementById('${cardId}').style.display === 'none' ? 'block' : 'none'">
                <h3>${cl.name}</h3>
                <span class="zm-meta">${cl.domains} domains, ${cl.mailboxes} mailboxes ${cl.renewing_soon > 0 ? '<span class="badge badge-red">' + cl.renewing_soon + ' renewing soon</span>' : ''}</span>
            </div>
            <div id="${cardId}" style="display:none;">
                <div class="cancel-controls">
                    <button class="cancel-btn" onclick="cancelSelectedDomains('${cardId}')" id="cancel-btn-${cardId}" disabled>Cancel Selected Domains</button>
                    <span id="cancel-status-${cardId}"></span>
                </div>
                <table class="zm-domain-table">
                    <thead><tr>
                        <th><input type="checkbox" onchange="toggleZmSelectAll('${cardId}', this.checked)"></th>
                        <th>Domain</th>
                        <th>Mailboxes</th>
                        <th>Created</th>
                        <th>Next Renewal</th>
                        <th>Status</th>
                    </tr></thead>
                    <tbody>
                    ${cl.domain_list.map(dm => {
                        var renewBadge = dm.days_until_renewal !== null && dm.days_until_renewal <= 3
                            ? '<span class="badge badge-red">' + dm.days_until_renewal + 'd</span>'
                            : (dm.days_until_renewal !== null ? dm.days_until_renewal + 'd' : '');
                        return `<tr>
                            <td><input type="checkbox" class="zm-check-${cardId}" value="${dm.id}" onchange="updateZmCancelBtn('${cardId}')"></td>
                            <td>${dm.domain}</td>
                            <td>${dm.mailbox_count}</td>
                            <td>${dm.created}</td>
                            <td>${dm.next_renewal || '?'} ${renewBadge}</td>
                            <td style="color:${dm.status === 'ACTIVE' ? '#22c55e' : '#ef4444'}">${dm.status}</td>
                        </tr>`;
                    }).join('')}
                    </tbody>
                </table>
            </div>
        </div>`;
    }).join('');
}

function toggleZmSelectAll(cardId, checked) {
    document.querySelectorAll('.zm-check-' + cardId).forEach(cb => cb.checked = checked);
    updateZmCancelBtn(cardId);
}

function updateZmCancelBtn(cardId) {
    var selected = document.querySelectorAll('.zm-check-' + cardId + ':checked').length;
    var btn = document.getElementById('cancel-btn-' + cardId);
    if (btn) btn.disabled = selected === 0;
}

async function cancelSelectedDomains(cardId) {
    var domainIds = Array.from(document.querySelectorAll('.zm-check-' + cardId + ':checked')).map(cb => cb.value);
    if (!domainIds.length) return;

    if (!confirm('Cancel ' + domainIds.length + ' domain(s) from ZapMail? This stops billing but domains stay on Spaceship.')) return;

    var btn = document.getElementById('cancel-btn-' + cardId);
    var status = document.getElementById('cancel-status-' + cardId);
    btn.disabled = true;
    status.textContent = 'Cancelling...';

    try {
        var result = await apiPost('/api/zapmail/cancel', {domain_ids: domainIds});
        if (result.error) {
            status.textContent = 'Error: ' + result.error.substring(0, 100);
        } else {
            status.textContent = 'Cancelled! Refreshing...';
            zmData = null;
            setTimeout(loadZapmail, 1500);
        }
    } catch (err) {
        status.textContent = 'Error: ' + err.message;
    }
}

// --- Domains (Registrars) ---

async function loadDomains() {
    document.getElementById('dom-content').innerHTML = '<div class="loading"><span class="spinner"></span> Loading domain registrar data...</div>';
    loadAgingPool();
    try {
        var resp = await fetch('/api/domains');
        domData = await resp.json();
        renderDomains();
    } catch (err) {
        document.getElementById('dom-content').innerHTML = 'Error: ' + err.message;
    }
}

function renderDomains() {
    var d = domData;

    document.getElementById('dom-summary-row').innerHTML =
        statCard(d.total_domains, 'Total Domains', 'good') +
        statCard(d.expiring_soon, 'Expiring in 14 days', d.expiring_soon > 0 ? 'alert' : 'good') +
        statCard(d.no_auto_renew_30d, 'No Auto-Renew (30d)', d.no_auto_renew_30d > 0 ? 'warn' : 'good');

    // Alerts
    if (d.alerts.length > 0) {
        var alertHtml = '<div class="alert-banner"><h3>Domain Expiry Alerts</h3>';
        d.alerts.forEach(a => {
            alertHtml += `<div class="alert-item">${a.domain} (${a.registrar}) — expires ${a.expires}, ${a.days_until_expiry} days left, auto-renew OFF</div>`;
        });
        alertHtml += '</div>';
        document.getElementById('dom-alerts').innerHTML = alertHtml;
    } else {
        document.getElementById('dom-alerts').innerHTML = '';
    }

    // Collect all auto-renew-ON domains for bulk action
    var allAutoRenewOn = [];
    for (var domains of Object.values(d.by_registrar)) {
        domains.forEach(dm => {
            if (dm.auto_renew) allAutoRenewOn.push({domain: dm.domain, registrar: dm.registrar});
        });
    }

    var html = '';
    if (allAutoRenewOn.length > 0) {
        html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;padding:12px 16px;background:var(--yellow-bg);border:1px solid var(--yellow);border-radius:var(--radius);font-size:13px;">' +
            '<span style="color:var(--yellow);font-weight:600;">' + allAutoRenewOn.length + ' domains have auto-renew ON</span>' +
            '<button id="bulk-disable-btn" onclick="bulkDisableAutoRenew()" style="background:var(--red);color:#fff;border:none;padding:6px 16px;border-radius:var(--radius);cursor:pointer;font-weight:600;font-size:12px;">Disable All Auto-Renew</button>' +
        '</div>';
    }

    // Domain tables by registrar
    for (var [registrar, domains] of Object.entries(d.by_registrar)) {
        html += '<h2 class="section-title">' + registrar + ' (' + domains.length + ' domains)</h2>';
        html += '<table class="zm-domain-table"><thead><tr><th style="width:30px;"><input type="checkbox" onchange="toggleSelectAllDomains(this,\'' + registrar + '\')" title="Select all"></th><th>Domain</th><th>Status</th><th>Expires</th><th>Days Left</th><th>Auto-Renew</th></tr></thead><tbody>';
        domains.forEach(dm => {
            var daysLeft = dm.days_until_expiry;
            var daysColor = daysLeft === null ? '#9ca3af' : (daysLeft <= 7 ? '#ef4444' : (daysLeft <= 30 ? '#f59e0b' : '#22c55e'));
            var renewColor = dm.auto_renew ? '#22c55e' : '#f59e0b';
            var toggleBtn = '<button onclick="toggleAutoRenew(\'' + dm.domain + '\',\'' + dm.registrar + '\',' + (!dm.auto_renew) + ',this)" style="background:none;border:1px solid ' + renewColor + ';color:' + renewColor + ';padding:2px 8px;border-radius:4px;cursor:pointer;font-size:12px;">' + (dm.auto_renew ? 'ON' : 'OFF') + '</button>';
            html += '<tr>' +
                '<td><input type="checkbox" class="dom-select" data-domain="' + dm.domain + '" data-registrar="' + dm.registrar + '" data-autorenew="' + dm.auto_renew + '"></td>' +
                '<td>' + dm.domain + '</td>' +
                '<td style="color:' + (dm.status === 'ACTIVE' || dm.status === 'registered' ? '#22c55e' : '#f59e0b') + '">' + dm.status + '</td>' +
                '<td>' + (dm.expires || '?') + '</td>' +
                '<td style="color:' + daysColor + '">' + (daysLeft !== null ? daysLeft + 'd' : '?') + '</td>' +
                '<td>' + toggleBtn + '</td>' +
            '</tr>';
        });
        html += '</tbody></table>';
    }
    document.getElementById('dom-content').innerHTML = html;
}

async function toggleAutoRenew(domain, registrar, enabled, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    try {
        var result = await apiPost('/api/domains/auto-renew', {domain: domain, registrar: registrar, enabled: enabled});
        if (result.success) {
            btn.textContent = enabled ? 'ON' : 'OFF';
            btn.style.color = enabled ? '#22c55e' : '#f59e0b';
            btn.style.borderColor = enabled ? '#22c55e' : '#f59e0b';
            btn.disabled = false;
            btn.onclick = () => toggleAutoRenew(domain, registrar, !enabled, btn);
            showToast(`Auto-renew ${enabled ? 'enabled' : 'disabled'} for ${domain}`, 'success');
        } else {
            showToast('Failed: ' + result.message, 'error');
            btn.textContent = enabled ? 'OFF' : 'ON';
            btn.disabled = false;
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
        btn.textContent = enabled ? 'OFF' : 'ON';
        btn.disabled = false;
    }
}

function toggleSelectAllDomains(checkbox, registrar) {
    document.querySelectorAll('.dom-select').forEach(cb => {
        if (cb.dataset.registrar === registrar) cb.checked = checkbox.checked;
    });
}

async function bulkDisableAutoRenew() {
    var domains = [];
    document.querySelectorAll('.dom-select').forEach(cb => {
        if (cb.dataset.autorenew === 'true') {
            domains.push({domain: cb.dataset.domain, registrar: cb.dataset.registrar});
        }
    });
    if (!domains.length) { showToast('No domains with auto-renew ON', 'warn'); return; }

    var btn = document.getElementById('bulk-disable-btn');
    btn.disabled = true;
    btn.textContent = `Disabling ${domains.length} domains...`;

    try {
        var result = await apiPost('/api/domains/bulk-auto-renew', {domains: domains.map(function(d) { return {domain: d.domain, registrar: d.registrar}; }), enabled: false});
        showToast(`Disabled: ${result.success} succeeded, ${result.failed} failed`, result.failed ? 'warn' : 'success', 5000);
        loadDomains();
    } catch (err) {
        showToast('Bulk disable error: ' + err.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Disable All Auto-Renew';
    }
}

// --- Sync Check ---

async function loadSync() {
    document.getElementById('sync-loading').style.display = 'block';
    document.getElementById('sync-content').innerHTML = '';
    try {
        var resp = await fetch('/api/zapmail/sync');
        syncData = await resp.json();
        renderSync();
    } catch (err) {
        document.getElementById('sync-content').innerHTML = 'Error: ' + err.message;
    }
    document.getElementById('sync-loading').style.display = 'none';
}

function renderSync() {
    var d = syncData;
    var html = '<div class="summary-row">' +
        statCard(d.total_checked, 'Domains Checked', 'good') +
        statCard(d.mismatches.length, 'Tag Mismatches', d.mismatches.length > 0 ? 'alert' : 'good') +
        statCard(d.zapmail_only_count, 'ZapMail Only', d.zapmail_only_count > 0 ? 'warn' : 'good') +
        statCard(d.smartlead_only_count, 'SmartLead Only', d.smartlead_only_count > 0 ? 'warn' : 'good') +
        '</div>';

    if (d.mismatches.length > 0) {
        html += '<h2 class="section-title">Tag Mismatches</h2>';
        d.mismatches.forEach(m => {
            html += `<div class="sync-item"><span class="domain">${m.domain}</span> — ZapMail: <span class="mismatch">${m.zapmail_tag}</span> vs SmartLead: <span class="mismatch">${m.smartlead_client}</span></div>`;
        });
    }

    [
        {title: 'In ZapMail but not SmartLead', items: d.zapmail_only, count: d.zapmail_only_count},
        {title: 'In SmartLead but not ZapMail', items: d.smartlead_only, count: d.smartlead_only_count}
    ].forEach(function(section) {
        if (section.count > 0) {
            html += '<h2 class="section-title">' + section.title + ' (' + section.count + ')</h2>';
            section.items.forEach(function(domain) {
                html += '<div class="sync-item"><span class="domain">' + domain + '</span></div>';
            });
            if (section.count > 20) html += '<div class="sync-item" style="color:var(--text-muted)">...and ' + (section.count - 20) + ' more</div>';
        }
    });

    if (d.mismatches.length === 0 && d.zapmail_only_count === 0 && d.smartlead_only_count === 0) {
        html += '<div class="sync-item" style="color:var(--accent);">Everything in sync!</div>';
    }

    document.getElementById('sync-content').innerHTML = html;
}

// --- Wallet ---
async function loadWallet() {
    try {
        var resp = await fetch('/api/wallet');
        var data = await resp.json();
        var balance = data.data?.balance || data.balance || '?';
        var el = document.getElementById('wallet-balance');
        var num = parseFloat(balance);
        el.textContent = '$' + (isNaN(num) ? '?' : num.toFixed(2));
        el.style.color = num < 50 ? '#ef4444' : num < 150 ? '#f59e0b' : '#22c55e';
    } catch(e) { console.error('Wallet error:', e); }
}
