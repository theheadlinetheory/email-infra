// ── Setup Forms (New Client, New Acquisition, Swap) ──

// --- New Client Form ---
function showNewClientForm() {
    document.getElementById('new-client-overlay').style.display = 'block';
    document.getElementById('new-client-form').style.display = 'block';
    document.getElementById('nc-status').textContent = '';
    document.getElementById('nc-generic').checked = false;
    document.getElementById('nc-forwarding-section').style.display = 'block';
    loadInventoryPreview();
}

function closeNewClientForm() {
    document.getElementById('new-client-overlay').style.display = 'none';
    document.getElementById('new-client-form').style.display = 'none';
}

async function swapClient(clientName) {
    if (!confirm('Swap ' + clientName + ' to the other group? This will update all their campaigns.')) return;
    var btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Swapping...';
    try {
        var result = await apiPost('/api/rotation/swap', {client_name: clientName});
        if (result.ok) {
            alert('Swapped ' + clientName + ' to Group ' + result.new_group + '.\n' + result.campaigns_updated + ' campaigns updated.');
            loadOverview();
        } else {
            alert('Swap failed: ' + (result.error || 'Unknown error'));
        }
    } catch(e) {
        alert('Swap error: ' + e.message);
    }
    btn.disabled = false;
}

async function swapAll() {
    if (!confirm('Swap ALL clients to their other group? This affects all campaigns.')) return;
    var btn = document.getElementById('swap-all-btn');
    btn.disabled = true;
    btn.textContent = 'Swapping All...';
    try {
        var result = await apiPost('/api/rotation/swap-all', {});
        var ok = (result.results || []).filter(r => r.ok).length;
        var fail = (result.results || []).filter(r => r.error).length;
        alert('Swap All complete: ' + ok + ' succeeded, ' + fail + ' failed.');
        loadOverview();
    } catch(e) {
        alert('Swap All error: ' + e.message);
    }
    btn.disabled = false;
    btn.textContent = 'Swap All Clients';
}

function toggleGenericMode() {
    var isGeneric = document.getElementById('nc-generic').checked;
    document.getElementById('nc-forwarding-section').style.display = isGeneric ? 'none' : 'block';
    if (isGeneric) document.getElementById('nc-forwarding').value = '';
}

async function fetchInventoryCount(displayElId) {
    var el = document.getElementById(displayElId);
    try {
        var resp = await fetch('/api/domain-inventory');
        var data = await resp.json();
        if (data.error) {
            el.textContent = 'Could not load inventory: ' + data.error;
            el.style.color = '#ef4444';
            return 0;
        }
        var count = data.available_count ?? 0;
        el.textContent = count + ' domains available in inventory';
        el.style.color = count < 5 ? '#ef4444' : '#22c55e';
        return count;
    } catch(e) {
        el.textContent = 'Could not load inventory';
        el.style.color = '#ef4444';
        return 0;
    }
}

async function loadInventoryPreview() {
    var count = await fetchInventoryCount('nc-inventory');
    if (count < 5) {
        var alertEl = document.getElementById('inventory-alert');
        alertEl.style.display = 'inline';
        alertEl.textContent = 'Low inventory: only ' + count + ' domains available';
    }
}

async function startNewClientPipeline() {
    var clientName = document.getElementById('nc-client-name').value.trim();
    var domainCount = parseInt(document.getElementById('nc-domain-count').value);
    var forwarding = document.getElementById('nc-forwarding').value.trim();

    if (!clientName) { alert('Client name required'); return; }
    if (!domainCount || domainCount < 1) { alert('Domain count required'); return; }

    document.getElementById('nc-start-btn').disabled = true;
    document.getElementById('nc-status').innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px;"></span> Starting pipeline...';

    try {
        var result = await apiPost('/api/pipeline/new-client', {client_name: clientName, domain_count: domainCount, forwarding_url: forwarding});
        if (result.error) {
            document.getElementById('nc-status').innerHTML = '<span style="color:var(--red);">' + result.error + '</span>';
            document.getElementById('nc-start-btn').disabled = false;
        } else {
            document.getElementById('nc-status').innerHTML = '<span style="color:var(--accent);">Pipeline started! ID: ' + result.pipeline_id + '</span>';
            setTimeout(function() {
                closeNewClientForm();
                switchTab('pipelines');
                loadPipelines();
            }, 1500);
        }
    } catch(err) {
        document.getElementById('nc-status').innerHTML = '<span style="color:var(--red);">Error: ' + err.message + '</span>';
        document.getElementById('nc-start-btn').disabled = false;
    }
}

// --- New Acquisition Group ---
function showNewAcquisitionForm() {
    document.getElementById('new-acq-overlay').style.display = 'block';
    document.getElementById('new-acq-form').style.display = 'block';
    document.getElementById('acq-status').textContent = '';
    document.getElementById('acq-group-name').value = '';
    document.getElementById('acq-daily-volume').value = '250';
    document.getElementById('acq-start-btn').disabled = false;
    updateAcqMath();
    loadAcqInventory();
}

function closeNewAcquisitionForm() {
    document.getElementById('new-acq-overlay').style.display = 'none';
    document.getElementById('new-acq-form').style.display = 'none';
}

function updateAcqMath() {
    var vol = parseInt(document.getElementById('acq-daily-volume').value) || 0;
    var senderKey = document.getElementById('acq-sender').value;
    var senderLabels = {aidan_hutchinson: 'Aidan Hutchinson', lars_matthys: 'Lars Matthys'};
    var accounts = Math.ceil(vol / 15);
    var domains = Math.ceil(accounts / 3);
    var actualAccounts = domains * 3;
    var actualDaily = actualAccounts * 15;
    document.getElementById('acq-math').innerHTML =
        `<strong>${domains}</strong> domains &times; 3 inboxes = <strong>${actualAccounts}</strong> accounts<br>` +
        `Actual capacity: <strong>${actualDaily}</strong> emails/day &bull; Sender: ${senderLabels[senderKey] || senderKey}`;
}

async function loadAcqInventory() {
    await fetchInventoryCount('acq-inventory');
}

async function startNewAcquisitionPipeline() {
    var groupName = document.getElementById('acq-group-name').value.trim();
    var dailyVolume = parseInt(document.getElementById('acq-daily-volume').value);

    if (!groupName) { alert('Group name required'); return; }
    if (!dailyVolume || dailyVolume < 15) { alert('Daily volume must be at least 15'); return; }

    document.getElementById('acq-start-btn').disabled = true;
    document.getElementById('acq-status').innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px;"></span> Starting pipeline...';

    try {
        var result = await apiPost('/api/pipeline/new-acquisition', {group_name: groupName, daily_volume: dailyVolume, sender: document.getElementById('acq-sender').value});
        if (result.error) {
            document.getElementById('acq-status').innerHTML = '<span style="color:var(--red);">' + result.error + '</span>';
            document.getElementById('acq-start-btn').disabled = false;
        } else {
            document.getElementById('acq-status').innerHTML = '<span style="color:var(--accent);">Pipeline started! ' + result.infra.domains_needed + ' domains, ' + result.infra.actual_accounts + ' inboxes</span>';
            setTimeout(function() {
                closeNewAcquisitionForm();
                switchTab('pipelines');
                loadPipelines();
            }, 1500);
        }
    } catch(err) {
        document.getElementById('acq-status').innerHTML = '<span style="color:var(--red);">Error: ' + err.message + '</span>';
        document.getElementById('acq-start-btn').disabled = false;
    }
}
