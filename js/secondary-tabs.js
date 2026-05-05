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
    var renewingSoon = d.clients.reduce((n, c) => n + c.renewing_soon, 0);
    document.getElementById('zm-summary-row').innerHTML = `
        <div class="stat-card good"><div class="value">${d.total_domains}</div><div class="label">Total Domains</div></div>
        <div class="stat-card good"><div class="value">${d.total_mailboxes}</div><div class="label">Total Mailboxes</div></div>
        <div class="stat-card good"><div class="value">${d.clients.length}</div><div class="label">Client Tags</div></div>
        <div class="stat-card ${renewingSoon > 0 ? 'alert' : 'good'}"><div class="value">${renewingSoon}</div><div class="label">Renewing in 3 days</div></div>
    `;

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

    document.getElementById('dom-summary-row').innerHTML = `
        <div class="stat-card good"><div class="value">${d.total_domains}</div><div class="label">Total Domains</div></div>
        <div class="stat-card ${d.expiring_soon > 0 ? 'alert' : 'good'}"><div class="value">${d.expiring_soon}</div><div class="label">Expiring in 14 days</div></div>
        <div class="stat-card ${d.no_auto_renew_30d > 0 ? 'warn' : 'good'}"><div class="value">${d.no_auto_renew_30d}</div><div class="label">No Auto-Renew (30d)</div></div>
    `;

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
    var html = '<div class="summary-row">';
    html += `<div class="stat-card good"><div class="value">${d.total_checked}</div><div class="label">Domains Checked</div></div>`;
    html += `<div class="stat-card ${d.mismatches.length > 0 ? 'alert' : 'good'}"><div class="value">${d.mismatches.length}</div><div class="label">Tag Mismatches</div></div>`;
    html += `<div class="stat-card ${d.zapmail_only_count > 0 ? 'warn' : 'good'}"><div class="value">${d.zapmail_only_count}</div><div class="label">ZapMail Only</div></div>`;
    html += `<div class="stat-card ${d.smartlead_only_count > 0 ? 'warn' : 'good'}"><div class="value">${d.smartlead_only_count}</div><div class="label">SmartLead Only</div></div>`;
    html += '</div>';

    if (d.mismatches.length > 0) {
        html += '<h2 class="section-title">Tag Mismatches</h2>';
        d.mismatches.forEach(m => {
            html += `<div class="sync-item"><span class="domain">${m.domain}</span> — ZapMail: <span class="mismatch">${m.zapmail_tag}</span> vs SmartLead: <span class="mismatch">${m.smartlead_client}</span></div>`;
        });
    }

    if (d.zapmail_only_count > 0) {
        html += `<h2 class="section-title">In ZapMail but not SmartLead (${d.zapmail_only_count})</h2>`;
        d.zapmail_only.forEach(domain => {
            html += `<div class="sync-item"><span class="domain">${domain}</span></div>`;
        });
        if (d.zapmail_only_count > 20) html += `<div class="sync-item" style="color:var(--text-muted)">...and ${d.zapmail_only_count - 20} more</div>`;
    }

    if (d.smartlead_only_count > 0) {
        html += `<h2 class="section-title">In SmartLead but not ZapMail (${d.smartlead_only_count})</h2>`;
        d.smartlead_only.forEach(domain => {
            html += `<div class="sync-item"><span class="domain">${domain}</span></div>`;
        });
        if (d.smartlead_only_count > 20) html += `<div class="sync-item" style="color:var(--text-muted)">...and ${d.smartlead_only_count - 20} more</div>`;
    }

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
