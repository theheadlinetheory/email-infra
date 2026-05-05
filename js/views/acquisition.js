/**
 * Acquisition view — groups, campaign assignment, conflicts, rotation, unassigned inboxes.
 * Full feature parity with old overview.js acquisition sections.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost, apiGet } from '../core/api.js';
import { showToast } from '../components/toast.js';

let container = null;
let unsubs = [];
let campaignsCache = null;

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('acquisition', render));
  unsubs.push(store.subscribe('acquisitionCampaigns', render));
  unsubs.push(store.subscribe('loading', render));
  unsubs.push(store.subscribe('errors', render));
  load();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  container = null;
  campaignsCache = null;
}

async function load() {
  try {
    await Promise.all([
      fetchSlice('acquisition', '/api/acquisition'),
      loadCampaigns(),
    ]);
  } catch (e) { /* handled by store */ }
}

async function loadCampaigns() {
  try {
    const resp = await apiGet('/api/acquisition-campaigns');
    campaignsCache = resp?.campaigns || resp || [];
    store.set('acquisitionCampaigns', campaignsCache);
  } catch (e) {
    campaignsCache = [];
  }
}

function render() {
  if (!container) return;
  const data = store.get('acquisition');
  const loading = store.get('loading')?.acquisition;
  const error = store.get('errors')?.acquisition;

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading acquisition data...</div>';
    return;
  }
  if (error && !data) {
    container.innerHTML = `<div class="error-card"><div class="error-msg">${esc(error)}</div><button class="retry-btn">Retry</button></div>`;
    container.querySelector('.retry-btn')?.addEventListener('click', load);
    return;
  }
  if (!data) return;

  const groups = data.groups || [];
  const totalAccounts = data.total_accounts || 0;
  const totalGroups = data.total_groups || 0;
  const conflicts = data.campaign_conflicts || [];
  const emptyCampaigns = data.empty_campaigns || [];
  const unassignedAccounts = data.unassigned_accounts || [];
  const campaigns = campaignsCache || [];

  let html = '';

  // --- Summary Cards ---
  html += '<div class="summary-row">';
  html += statCardHtml(totalAccounts, 'Acquisition Inboxes', 'good');
  html += statCardHtml(totalGroups, 'Active Groups', 'good');
  html += '</div>';

  // --- Conflict Alerts ---
  if (conflicts.length > 0 || emptyCampaigns.length > 0) {
    if (conflicts.length > 0) {
      html += '<div class="alert-banner" style="border-color:#fecaca;"><h3 style="color:#dc2626;">Campaign Conflicts</h3>';
      conflicts.forEach(c => {
        html += `<div class="alert-item" style="color:#dc2626;">${esc(c.group)} is in ${c.campaigns.length} active campaigns: ${esc(c.campaigns.join(', '))}</div>`;
      });
      html += '</div>';
    }
    if (emptyCampaigns.length > 0) {
      html += '<div class="alert-banner" style="border-color:#fed7aa;"><h3 style="color:#ea580c;">Campaigns With No Inboxes</h3>';
      emptyCampaigns.forEach(c => {
        html += `<div class="alert-item" style="color:#ea580c;">${esc(c.name)} — active but has no email accounts assigned</div>`;
      });
      html += '</div>';
    }
  }

  // --- Swap All Button ---
  if (groups.length > 1) {
    html += `<div style="display:flex;justify-content:flex-end;margin-bottom:12px;">
      <button id="swap-all-btn" style="background:var(--purple);color:#fff;border:none;padding:8px 18px;border-radius:var(--radius);cursor:pointer;font-weight:600;font-size:13px;">Swap All A/B</button>
    </div>`;
  }

  // --- Acquisition Group Cards ---
  html += '<div id="acquisition-grid">';
  groups.forEach(g => {
    html += renderGroupCard(g, campaigns);
  });
  html += '</div>';

  // --- Unassigned Acquisition Inboxes ---
  if (unassignedAccounts.length > 0) {
    html += '<div style="margin-top:24px;">';
    html += `<h2 class="section-title">${unassignedAccounts.length} Unassigned Acquisition Inbox(es)</h2>`;
    html += `<p style="font-size:13px;color:var(--text-muted);margin-bottom:12px;">Inboxes with Headline Theory domains not assigned to any acquisition group</p>`;
    html += '<table class="zm-domain-table"><thead><tr>';
    html += '<th>Email</th><th>From Name</th><th>Domain</th><th>Warmup</th><th>Reputation</th><th>SMTP</th>';
    html += '</tr></thead><tbody>';
    unassignedAccounts.forEach(a => {
      html += '<tr>';
      html += `<td>${esc(a.email)}</td>`;
      html += `<td>${esc(a.from_name || '-')}</td>`;
      html += `<td>${esc(a.domain)}</td>`;
      html += `<td style="color:${a.warmup_status === 'ACTIVE' ? '#22c55e' : '#f59e0b'}">${esc(a.warmup_status || '')}</td>`;
      html += `<td>${esc(String(a.warmup_reputation ?? ''))}</td>`;
      html += `<td style="color:${a.smtp_ok ? '#22c55e' : '#ef4444'}">${a.smtp_ok ? 'OK' : 'FAIL'}</td>`;
      html += '</tr>';
    });
    html += '</tbody></table></div>';
  }

  container.innerHTML = html;
  bindEvents(groups, campaigns);
}

function renderGroupCard(g, campaigns) {
  const name = g.name || g.group_name || '';
  const healthColor = (g.avg_health || 0) >= 90 ? 'var(--green)' :
                      (g.avg_health || 0) >= 75 ? 'var(--yellow)' : 'var(--red)';
  const healthPct = Math.round(g.avg_health || 0);

  const activeCampaigns = g.active_campaigns || [];
  const pausedCampaigns = g.paused_campaigns || [];
  const hasConflict = g.campaign_conflict || activeCampaigns.length > 1;
  const currentCampId = activeCampaigns.length === 1 ? activeCampaigns[0].id : null;

  let html = `<div class="client-card" style="margin-bottom:12px;">`;

  // Header
  html += `<div class="cc-header">
    <span class="cc-name">${esc(name)}</span>
    <span style="color:${healthColor};font-weight:600">${healthPct}%</span>
  </div>`;

  // Alert banner for flagged domains
  if (g.needs_attention) {
    html += `<div style="background:var(--red-bg);border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:12px;color:var(--red);">${g.flagged_domains || 0}/${g.total_domains || 0} domains flagged (${g.flagged_pct || 0}%)</div>`;
  }

  // Stats row
  const capacityDisplay = g.still_warming && g.daily_capacity < g.projected_capacity
    ? (g.daily_capacity || 0) + ' → ' + g.projected_capacity + '/day'
    : (g.daily_capacity || 0) + '/day';

  html += '<div class="cc-stats">';
  html += `<div class="cc-stat"><span class="label">Accounts</span><span>${g.account_count || g.accounts || 0}</span></div>`;
  html += `<div class="cc-stat"><span class="label">Capacity</span><span>${capacityDisplay}</span></div>`;
  html += `<div class="cc-stat"><span class="label">In Campaign</span><span>${g.in_campaign || 0}</span></div>`;
  html += `<div class="cc-stat"><span class="label">Warmup</span><span>${g.in_warmup || 0}</span></div>`;

  // Bounce/reply rates if available
  if (g.avg_bounce_rate !== undefined && g.avg_bounce_rate !== null) {
    const bounceColor = g.avg_bounce_rate > 3 ? '#ef4444' : g.avg_bounce_rate > 1 ? '#f59e0b' : 'var(--accent)';
    html += `<div class="cc-stat"><span class="label">Bounce Rate</span><span style="color:${bounceColor}">${g.avg_bounce_rate}%</span></div>`;
  }
  if (g.avg_reply_rate !== undefined && g.avg_reply_rate !== null) {
    const replyColor = g.avg_reply_rate > 5 ? 'var(--accent)' : g.avg_reply_rate > 2 ? '#f59e0b' : '#ef4444';
    html += `<div class="cc-stat"><span class="label">Reply Rate</span><span style="color:${replyColor}">${g.avg_reply_rate}%</span></div>`;
  }
  html += '</div>';

  // Batch warmup bars
  if (g.batches && g.batches.length > 0) {
    const warmingBatches = g.batches.filter(b => b.status === 'warming');
    const readyBatches = g.batches.filter(b => b.status === 'ready');
    if (warmingBatches.length > 0 || readyBatches.length > 1) {
      html += '<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;">';
      for (const b of g.batches) {
        if (b.status === 'ready') {
          html += `<div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:4px;"><span style="color:var(--accent);">&#9679; ${esc(String(b.total))} accounts ready</span><span style="color:var(--text-muted);">since ${esc(b.warmup_start || '')}</span></div>`;
        } else {
          const pct = Math.round((b.days_done || 0) / 14 * 100);
          html += `<div style="margin-bottom:6px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:3px;"><span style="color:var(--purple);">&#9679; ${esc(String(b.total))} new accounts warming</span><span>Day ${b.days_done || 0}/14</span></div><div style="background:var(--bg-input);border-radius:4px;height:5px;overflow:hidden;"><div style="background:var(--purple);height:100%;width:${pct}%;border-radius:4px;"></div></div></div>`;
        }
      }
      html += '</div>';
    }
  }

  // Campaign assignment section
  html += '<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;font-size:12px;">';
  if (hasConflict) {
    html += `<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:6px 10px;margin-bottom:6px;color:#dc2626;font-weight:600;">CONFLICT: ${activeCampaigns.length} active campaigns</div>`;
    activeCampaigns.forEach(c => {
      html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;">
        <span style="color:#dc2626;">&#9679; ${esc(c.name)}</span>
        <button class="acq-unassign-btn" data-group-id="${g.id}" data-group-name="${esc(name)}" data-camp-id="${c.id}" data-camp-name="${esc(c.name)}" style="font-size:10px;padding:2px 8px;border:1px solid #fecaca;border-radius:4px;background:#fef2f2;color:#dc2626;cursor:pointer;">Remove</button>
      </div>`;
    });
  } else {
    html += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
      <span class="label" style="white-space:nowrap;">Campaign</span>
      <select class="acq-campaign-select" data-group-id="${g.id}" data-group-name="${esc(name)}" data-current-camp="${currentCampId || 0}" style="flex:1;font-size:12px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-input);color:var(--text-primary);max-width:200px;cursor:pointer;">
        <option value="">— Available —</option>
        ${campaigns.map(c => {
          const selected = c.id === currentCampId ? ' selected' : '';
          const label = c.name + (c.status === 'PAUSED' ? ' (paused)' : '');
          return `<option value="${c.id}"${selected}>${esc(label)}</option>`;
        }).join('')}
      </select>
    </div>`;
  }
  if (pausedCampaigns.length > 0) {
    html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
      <span class="label">Paused</span>
      <span style="color:var(--text-muted);">${esc(pausedCampaigns.map(c => c.name).join(', '))}</span>
    </div>`;
  }
  html += '</div>';

  // Footer dates (ready/rotation)
  const hasReady = g.ready_date && g.days_until_ready !== null && g.days_until_ready !== undefined && g.days_until_ready > 0;
  const hasRotation = !!g.rotation_date;
  if (hasReady || hasRotation) {
    html += '<div class="cc-dates">';
    if (hasReady) {
      const readyBadge = g.days_until_ready <= 0
        ? '<span class="badge badge-green">Ready</span>'
        : `<span class="badge badge-yellow">${g.days_until_ready}d left</span>`;
      html += `<div class="date-row"><span>Ready Date</span><span>${esc(g.ready_date)} ${readyBadge}</span></div>`;
    }
    if (hasRotation) {
      const rotBadge = g.days_until_rotation !== null && g.days_until_rotation !== undefined && g.days_until_rotation <= 7
        ? ' <span class="badge badge-red">Rotate soon</span>' : '';
      html += `<div class="date-row"><span>Rotation Date</span><span>${esc(g.rotation_date)}${rotBadge}</span></div>`;
    }
    html += '</div>';
  }

  // Swap button per group
  if (g.rotation_enabled || g.group_a_ids || g.group_b_ids) {
    const activeGroup = g.active_group || 'A';
    const otherGroup = activeGroup === 'A' ? 'B' : 'A';
    html += `<div style="margin-top:8px;text-align:right;">
      <button class="acq-swap-btn action-btn secondary" data-client-name="${esc(name)}" style="font-size:11px;">Swap to Group ${otherGroup}</button>
    </div>`;
  }

  html += '</div>';
  return html;
}

function bindEvents(groups, campaigns) {
  if (!container) return;

  // Campaign assignment dropdowns
  container.querySelectorAll('.acq-campaign-select').forEach(select => {
    select.addEventListener('change', () => {
      const groupId = parseInt(select.dataset.groupId);
      const groupName = select.dataset.groupName;
      const newCampId = select.value ? parseInt(select.value) : null;
      const currentCampId = parseInt(select.dataset.currentCamp) || null;
      handleAssignCampaign(groupId, groupName, newCampId, currentCampId);
    });
  });

  // Unassign (remove) conflict buttons
  container.querySelectorAll('.acq-unassign-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const groupId = parseInt(btn.dataset.groupId);
      const groupName = btn.dataset.groupName;
      const campId = parseInt(btn.dataset.campId);
      const campName = btn.dataset.campName;
      handleUnassignCampaign(groupId, groupName, campId, campName);
    });
  });

  // Swap buttons per group
  container.querySelectorAll('.acq-swap-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      handleSwap(btn.dataset.clientName);
    });
  });

  // Swap all button
  const swapAllBtn = container.querySelector('#swap-all-btn');
  if (swapAllBtn) {
    swapAllBtn.addEventListener('click', handleSwapAll);
  }
}

async function handleAssignCampaign(groupId, groupName, newCampId, currentCampId) {
  // Unassign from current campaign first if changing
  if (currentCampId && currentCampId !== newCampId) {
    if (!confirm(`Remove ${groupName} from current campaign before assigning to new one?`)) {
      reload();
      return;
    }
    try {
      await apiPost('/api/acquisition/assign-campaign', {
        group_client_id: groupId,
        group_name: groupName,
        campaign_id: currentCampId,
        action: 'unassign',
      });
    } catch (e) {
      showToast('Failed to unassign: ' + e.message, 'error');
      return;
    }
  }

  // Assign to new campaign
  if (newCampId) {
    const campName = campaignsCache
      ? (campaignsCache.find(c => c.id === newCampId) || {}).name || ''
      : '';
    if (!confirm(`Assign all ${groupName} accounts to "${campName}"?`)) {
      reload();
      return;
    }
    try {
      const result = await apiPost('/api/acquisition/assign-campaign', {
        group_client_id: groupId,
        group_name: groupName,
        campaign_id: newCampId,
        action: 'assign',
      });
      if (result.error) {
        showToast('Error: ' + result.error, 'error');
      } else {
        showToast(`Assigned ${groupName} to ${campName}`, 'success');
      }
    } catch (e) {
      showToast('Failed to assign: ' + e.message, 'error');
    }
  }

  reload();
}

async function handleUnassignCampaign(groupId, groupName, campId, campName) {
  if (!confirm(`Remove ${groupName} from "${campName}"?`)) return;
  try {
    const result = await apiPost('/api/acquisition/assign-campaign', {
      group_client_id: groupId,
      group_name: groupName,
      campaign_id: campId,
      action: 'unassign',
    });
    if (result.error) {
      showToast('Error: ' + result.error, 'error');
    } else {
      showToast(`Removed ${groupName} from ${campName}`, 'success');
    }
  } catch (e) {
    showToast('Failed to unassign: ' + e.message, 'error');
  }
  reload();
}

async function handleSwap(clientName) {
  if (!confirm(`Swap A/B groups for ${clientName}?`)) return;
  try {
    const result = await apiPost('/api/rotation/swap', { client_name: clientName });
    if (result.error) {
      showToast('Swap error: ' + result.error, 'error');
    } else {
      showToast(`Swapped groups for ${clientName}`, 'success');
    }
  } catch (e) {
    showToast('Swap failed: ' + e.message, 'error');
  }
  reload();
}

async function handleSwapAll() {
  if (!confirm('Swap A/B for ALL acquisition groups?')) return;
  const btn = container?.querySelector('#swap-all-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Swapping...';
  }
  try {
    const result = await apiPost('/api/rotation/swap-all', {});
    if (result.error) {
      showToast('Swap all error: ' + result.error, 'error');
    } else {
      showToast('All groups swapped', 'success');
    }
  } catch (e) {
    showToast('Swap all failed: ' + e.message, 'error');
  }
  reload();
}

async function reload() {
  campaignsCache = null;
  try {
    await Promise.all([
      fetchSlice('acquisition', '/api/acquisition'),
      loadCampaigns(),
    ]);
  } catch (e) { /* handled */ }
}

function statCardHtml(value, label, variant = '') {
  return `<div class="stat-card ${esc(variant)}"><div class="value">${esc(String(value))}</div><div class="label">${esc(label)}</div></div>`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
