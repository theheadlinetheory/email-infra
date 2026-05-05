/**
 * Overview view — main dashboard with client cards, health stats, alerts,
 * mode switching (fulfillment/acquisition), generic groups, A/B rotation,
 * unassigned accounts, acquisition groups with campaign dropdowns,
 * setup pipelines, and generic setup tracker.
 *
 * Three-state rendering: loading skeleton -> error with retry -> data.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiGet, apiPost } from '../core/api.js';
import { statCard, statCardSkeleton } from '../components/stat-card.js';
import { dataTable } from '../components/data-table.js';
import { showToast } from '../components/toast.js';
import { openModal, closeModal } from '../components/modal.js';

/* ─── Module State ─── */

let container = null;
let unsubs = [];
let currentMode = localStorage.getItem('dashboardMode') || 'fulfillment';
let currentFilter = 'active';
let acqCampaignsCache = null;
let setupPipelinePollInterval = null;
let genericTrackerInterval = null;

/* ─── Lifecycle ─── */

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('overview', render));
  unsubs.push(store.subscribe('loading', render));
  unsubs.push(store.subscribe('errors', render));
  unsubs.push(store.subscribe('unassigned', render));
  unsubs.push(store.subscribe('genericGroups', render));
  unsubs.push(store.subscribe('acquisition', render));
  unsubs.push(store.subscribe('domainInventory', render));
  unsubs.push(store.subscribe('rotationStatus', render));
  unsubs.push(store.subscribe('setupPipelines', render));
  load();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  container = null;
  if (setupPipelinePollInterval) { clearInterval(setupPipelinePollInterval); setupPipelinePollInterval = null; }
  if (genericTrackerInterval) { clearInterval(genericTrackerInterval); genericTrackerInterval = null; }
  acqCampaignsCache = null;
}

/* ─── Data Loading ─── */

async function load() {
  const fetches = [
    fetchSlice('overview', '/api/overview').catch(() => null),
    fetchSlice('unassigned', '/api/unassigned').catch(() => null),
    fetchSlice('genericGroups', '/api/generic-groups').catch(() => null),
    fetchSlice('acquisition', '/api/acquisition').catch(() => null),
    fetchSlice('domainInventory', '/api/domain-inventory').catch(() => null),
    fetchSlice('rotationStatus', '/api/rotation/status').catch(() => null),
    fetchSlice('setupPipelines', '/api/setup-pipelines').catch(() => null),
    loadUntaggedCount(),
    loadGenericSetupStatus(),
  ];
  await Promise.all(fetches);
}

let untaggedCount = 0;
async function loadUntaggedCount() {
  try {
    const data = await apiGet('/api/untagged-count');
    untaggedCount = data?.untagged_count || 0;
  } catch { untaggedCount = 0; }
}

let genericSetupData = null;
async function loadGenericSetupStatus() {
  try {
    const data = await apiGet('/api/generic-groups-status');
    genericSetupData = data;
  } catch { genericSetupData = null; }
}

async function reloadAcquisition() {
  acqCampaignsCache = null;
  await fetchSlice('acquisition', '/api/acquisition').catch(() => null);
}

/* ─── Helpers ─── */

function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function rateColor(value, thresholds) {
  if (value == null) return 'var(--text-muted)';
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
  return value != null ? value + '%' : '—';
}

function computeAverageRate(items, field) {
  const values = (items || []).filter(c => c[field] != null && c[field] > 0).map(c => c[field]);
  return values.length ? (values.reduce((a, b) => a + b, 0) / values.length).toFixed(1) : '—';
}

function rateCssClass(value, thresholds) {
  if (value === '—') return 'good';
  const n = parseFloat(value);
  if (thresholds.ascending) {
    if (n > thresholds.good) return 'good';
    if (n > thresholds.warn) return 'warn';
    return 'alert';
  }
  if (n > thresholds.bad) return 'alert';
  if (n > thresholds.warn) return 'warn';
  return 'good';
}

/* ─── Main Render ─── */

function render() {
  if (!container) return;
  const data = store.get('overview');
  const loading = store.get('loading')?.overview;
  const error = store.get('errors')?.overview;
  const meta = store.getMeta('overview');

  if (loading && !data) {
    container.innerHTML = '';
    renderLoading(container);
    return;
  }

  if (error && !data) {
    container.innerHTML = '';
    renderError(container, error);
    return;
  }

  if (!data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading overview...</div>';
    return;
  }

  container.innerHTML = '';
  renderData(container, data, meta);
}

function renderLoading(el) {
  const stats = document.createElement('div');
  stats.className = 'summary-row';
  for (let i = 0; i < 5; i++) stats.appendChild(statCardSkeleton());
  el.appendChild(stats);

  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  for (let i = 0; i < 6; i++) {
    const card = document.createElement('div');
    card.className = 'skeleton skeleton-card';
    card.style.height = '160px';
    grid.appendChild(card);
  }
  el.appendChild(grid);
}

function renderError(el, error) {
  const card = document.createElement('div');
  card.className = 'error-card';
  card.innerHTML = `
    <div class="error-msg">${esc(error)}</div>
    <button class="retry-btn">Retry</button>
  `;
  card.querySelector('.retry-btn').addEventListener('click', () => {
    card.querySelector('.retry-btn').disabled = true;
    load();
  });
  el.appendChild(card);
}

/* ─── Data Rendering ─── */

function renderData(el, data, meta) {
  const inventoryData = store.get('domainInventory');

  // ── Header bar: timestamp + inventory badges + mode toggle ──
  const headerBar = document.createElement('div');
  headerBar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px;';
  headerBar.innerHTML = buildHeaderBar(data, inventoryData, meta);
  headerBar.querySelectorAll('.mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      currentMode = btn.dataset.mode;
      localStorage.setItem('dashboardMode', currentMode);
      render();
    });
  });
  el.appendChild(headerBar);

  // ── Stale badge ──
  if (meta?.cached) {
    const badge = document.createElement('div');
    badge.className = 'stale-badge';
    badge.textContent = `Updated ${meta.stale_seconds}s ago`;
    badge.style.cssText = 'margin-bottom:12px;cursor:pointer;';
    badge.addEventListener('click', load);
    el.appendChild(badge);
  }

  // ── Untagged alert ──
  if (currentMode === 'fulfillment' && untaggedCount > 0) {
    const alert = document.createElement('div');
    alert.className = 'alert-banner';
    alert.style.cssText = 'display:flex;align-items:center;gap:8px;';
    alert.innerHTML = `<span style="font-size:14px;">&#9888; ${esc(untaggedCount)} accounts have no client assignment and may be missing tags. Run fix_untagged.py to remediate.</span>`;
    el.appendChild(alert);
  }

  // ── Alert banner (fulfillment only) ──
  renderAlertBanner(el, data, inventoryData);

  // ── Summary stats row ──
  const summaryRow = document.createElement('div');
  summaryRow.className = 'summary-row';
  buildSummaryStats(summaryRow, data);
  el.appendChild(summaryRow);

  // ── Mode-specific content ──
  if (currentMode === 'fulfillment') {
    renderFulfillmentMode(el, data);
  } else {
    renderAcquisitionMode(el, data);
  }
}

/* ─── Header Bar ─── */

function buildHeaderBar(data, inventoryData, meta) {
  const time = data.generated_at ? new Date(data.generated_at).toLocaleTimeString() : '';

  let inventoryBadges = '';
  if (inventoryData) {
    const clientCls = inventoryData.client_low ? 'badge-red' : 'badge-green';
    const acqCls = inventoryData.acquisition_low ? 'badge-red' : 'badge-green';
    inventoryBadges = `
      <span class="badge ${clientCls}">Client: ${esc(inventoryData.client_available)}</span>
      <span class="badge ${acqCls}">Acq: ${esc(inventoryData.acquisition_available)}</span>
    `;
  }

  const fulfillActive = currentMode === 'fulfillment' ? 'active' : '';
  const acqActive = currentMode === 'acquisition' ? 'active' : '';

  return `
    <div style="display:flex;align-items:center;gap:12px;">
      <span style="color:var(--text-muted);font-size:13px;">Updated: ${esc(time)}</span>
      ${inventoryBadges}
    </div>
    <div class="mode-switcher">
      <button class="mode-btn ${fulfillActive}" data-mode="fulfillment">Fulfillment</button>
      <button class="mode-btn ${acqActive}" data-mode="acquisition">Acquisition</button>
    </div>
  `;
}

/* ─── Summary Stats ─── */

function buildSummaryStats(summaryRow, data) {
  const acqData = store.get('acquisition');
  let items, countVal1, countLabel1, countVal2, countLabel2;

  if (currentMode === 'fulfillment') {
    items = data.clients || [];
    countVal1 = data.total_accounts || 0;
    countLabel1 = 'Total Accounts';
    countVal2 = data.in_campaign || 0;
    countLabel2 = 'In Campaigns';
  } else {
    if (!acqData) return;
    items = acqData.groups || [];
    countVal1 = acqData.total_accounts || 0;
    countLabel1 = 'Total Accounts';
    countVal2 = acqData.total_groups || 0;
    countLabel2 = 'Active Groups';
  }

  const avgBounce = computeAverageRate(items, 'avg_bounce_rate');
  const avgReply = computeAverageRate(items, 'avg_reply_rate');
  const bounceClass = rateCssClass(avgBounce, { bad: 3, warn: 1 });
  const replyClass = rateCssClass(avgReply, { ascending: true, good: 5, warn: 2 });
  const bounceSuffix = avgBounce !== '—' ? '%' : '';
  const replySuffix = avgReply !== '—' ? '%' : '';

  summaryRow.appendChild(statCard({ value: countVal1, label: countLabel1, variant: 'good' }));
  summaryRow.appendChild(statCard({ value: countVal2, label: countLabel2, variant: 'good' }));
  summaryRow.appendChild(statCard({ value: avgBounce + bounceSuffix, label: 'Avg Bounce Rate', variant: bounceClass }));
  summaryRow.appendChild(statCard({ value: avgReply + replySuffix, label: 'Avg Reply Rate', variant: replyClass }));
}

/* ─── Alert Banner ─── */

function renderAlertBanner(el, data, inventoryData) {
  if (currentMode !== 'fulfillment') return;

  const attentionClients = (data.clients || []).filter(c => c.needs_attention);
  const invLow = inventoryData && (inventoryData.client_low || inventoryData.acquisition_low);
  const blocked = data.blocked_accounts || [];
  const hasAlerts = blocked.length > 0 || (data.smtp_failures || 0) > 0 ||
    attentionClients.length > 0 || (data.idle_inboxes || 0) > 0 || invLow;

  if (!hasAlerts) return;

  const banner = document.createElement('div');
  banner.className = 'alert-banner';
  let html = '<h3>Alerts</h3>';

  if (inventoryData?.client_low) {
    html += `<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Client pool has ${esc(inventoryData.client_available)} available (need 20+)</div>`;
  }
  if (inventoryData?.acquisition_low) {
    html += `<div class="alert-item" style="color:var(--yellow);">Domain inventory low: Acquisition pool has ${esc(inventoryData.acquisition_available)} available (need 20+)</div>`;
  }
  if ((data.idle_inboxes || 0) > 0) {
    html += `<div class="alert-item" style="font-size:14px;margin-bottom:6px;color:var(--yellow);">${esc(data.idle_inboxes)} warmed inbox(es) across ${esc(data.idle_clients)} client(s) are not in any campaign</div>`;
  }
  if (attentionClients.length > 0) {
    html += `<div class="alert-item" style="font-size:14px;margin-bottom:6px;">${attentionClients.length} client(s) have infrastructure that needs attention</div>`;
    attentionClients.forEach(c => {
      html += `<div class="alert-item" style="padding-left:16px;">${esc(c.name)} — ${esc(c.flagged_domains)}/${esc(c.total_domains)} domains flagged (health score: ${esc(c.health_score)})</div>`;
    });
  }
  if ((data.smtp_failures || 0) > 0) {
    html += `<div class="alert-item">${esc(data.smtp_failures)} accounts with SMTP failures</div>`;
  }
  if ((data.imap_failures || 0) > 0) {
    html += `<div class="alert-item">${esc(data.imap_failures)} accounts with IMAP failures</div>`;
  }

  // Group blocked accounts by reason
  const grouped = {};
  blocked.forEach(b => {
    const short = (b.reason || 'Unknown').split(':')[0];
    if (!grouped[short]) grouped[short] = [];
    grouped[short].push((b.email || '').split('@')[1]);
  });
  for (const [reason, domains] of Object.entries(grouped)) {
    const unique = [...new Set(domains)];
    html += `<div class="alert-item">${unique.length} domain(s) blocked — ${esc(reason)}: ${esc(unique.join(', '))}</div>`;
  }

  banner.innerHTML = html;
  el.appendChild(banner);
}

/* ─── Fulfillment Mode ─── */

function renderFulfillmentMode(el, data) {
  const archivedClients = data.archived_clients || [];
  const pausedClients = data.paused_clients || [];
  const activeClients = (data.clients || []).filter(cl => !archivedClients.includes(cl.name));
  const archivedClientData = (data.clients || []).filter(cl => archivedClients.includes(cl.name));

  // ── Subtitle + Filter bar ──
  const subtitle = document.createElement('div');
  subtitle.style.cssText = 'display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;';
  subtitle.innerHTML = `
    <span style="color:var(--text-secondary);font-size:14px;">${activeClients.length} active clients, ${esc(data.total_accounts)} accounts, ${esc(data.in_campaign)} in campaigns</span>
    <div class="filter-bar" style="display:flex;gap:4px;"></div>
  `;
  const filterBar = subtitle.querySelector('.filter-bar');
  const makeFilterPill = (label, count, filter) => {
    const btn = document.createElement('button');
    btn.className = `filter-pill ${currentFilter === filter ? 'active' : ''}`;
    btn.innerHTML = `${esc(label)} <span class="count">${count}</span>`;
    btn.addEventListener('click', () => {
      currentFilter = filter;
      render();
    });
    return btn;
  };
  filterBar.appendChild(makeFilterPill('Active', activeClients.length, 'active'));
  filterBar.appendChild(makeFilterPill('Archived', archivedClientData.length, 'archived'));
  el.appendChild(subtitle);

  // ── Client cards grid ──
  const clients = currentFilter === 'archived' ? archivedClientData : activeClients;
  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  clients.forEach(cl => grid.appendChild(buildClientCard(cl)));
  el.appendChild(grid);

  // ── Setup pipelines section ──
  renderSetupPipelinesSection(el);

  // ── Generic setup tracker ──
  renderGenericSetupTracker(el);

  // ── Generic groups section ──
  renderGenericGroupsSection(el);

  // ── A/B Rotation section ──
  renderRotationSection(el);

  // ── Unassigned accounts section ──
  renderUnassignedSection(el, data);
}

/* ─── Acquisition Mode ─── */

function renderAcquisitionMode(el, data) {
  const acqData = store.get('acquisition');
  if (!acqData || !acqData.total_groups) {
    const empty = document.createElement('div');
    empty.style.cssText = 'text-align:center;color:var(--text-muted);padding:40px;';
    empty.textContent = 'No acquisition groups found.';
    el.appendChild(empty);
    return;
  }

  // ── Acquisition stats ──
  const statsRow = document.createElement('div');
  statsRow.className = 'summary-row';
  statsRow.appendChild(statCard({ value: acqData.total_accounts, label: 'Acquisition Inboxes' }));
  statsRow.appendChild(statCard({ value: acqData.total_groups, label: 'Active Groups' }));
  el.appendChild(statsRow);

  // ── Conflict/empty campaign alerts ──
  renderAcqAlerts(el, acqData);

  // ── Acquisition group cards ──
  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  (acqData.groups || []).forEach(g => grid.appendChild(buildClientCard(g)));
  el.appendChild(grid);

  // Populate campaign dropdowns after DOM insertion
  setTimeout(() => populateCampaignDropdowns(el, acqData.groups || []), 0);
}

/* ─── Client Card Builder ─── */

function buildClientCard(item) {
  const card = document.createElement('div');
  card.className = `client-card ${item.needs_attention ? 'has-alert' : ''}`;
  card.style.cursor = 'pointer';

  const issues = (item.smtp_failures || 0) + (item.blocked || 0);
  const issuesColor = issues > 0 ? '#ef4444' : 'var(--accent)';
  const bounceVal = rateDisplay(item.avg_bounce_rate);
  const bounceColor = rateColor(item.avg_bounce_rate, { bad: 3, warn: 1 });
  const replyVal = rateDisplay(item.avg_reply_rate);
  const replyColor = rateColor(item.avg_reply_rate, { ascending: true, good: 5, warn: 2 });

  let html = '';

  // ── Header ──
  let warmupBadge = '';
  if (item.still_warming && item.warmup_done_date) {
    warmupBadge = `<span class="badge badge-yellow" style="font-size:10px;margin-left:6px;">Ready ${esc(item.warmup_done_date)}</span>`;
  }
  html += `<div class="cc-header"><span class="cc-name">${esc(item.name)}</span><span class="cc-count">${esc(item.accounts)} accounts${warmupBadge}</span></div>`;

  // ── Flagged domain alert ──
  if (item.needs_attention) {
    html += `<div style="background:var(--red-bg);border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:12px;color:var(--red);">${esc(item.flagged_domains)}/${esc(item.total_domains)} domains flagged (${esc(item.flagged_pct)}%)</div>`;
  }

  // ── Stats row ──
  let capacityDisplay = (item.daily_capacity || 0) + '/day';
  if (item.still_warming && item.daily_capacity < item.projected_capacity) {
    capacityDisplay = (item.daily_capacity || 0) + ' → ' + item.projected_capacity + '/day';
  }
  html += `<div class="cc-stats">`;
  html += `<div class="cc-stat"><span class="label">Capacity</span><span>${esc(capacityDisplay)}</span></div>`;
  html += `<div class="cc-stat"><span class="label">Issues</span><span style="color:${issuesColor}">${issues}</span></div>`;
  html += `<div class="cc-stat"><span class="label">Bounce Rate</span><span style="color:${bounceColor}">${bounceVal}</span></div>`;
  html += `<div class="cc-stat"><span class="label">Reply Rate</span><span style="color:${replyColor}">${replyVal}</span></div>`;
  html += `</div>`;

  // ── Batch warmup bars ──
  if (item.batches && item.batches.length > 0) {
    const warmingBatches = item.batches.filter(b => b.status === 'warming');
    const readyBatches = item.batches.filter(b => b.status === 'ready');
    if (warmingBatches.length > 0 || readyBatches.length > 1) {
      html += `<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;">`;
      for (const b of item.batches) {
        if (b.status === 'ready') {
          html += `<div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:4px;"><span style="color:var(--accent);">&#9679; ${esc(b.total)} accounts ready</span><span style="color:var(--text-muted);">since ${esc(b.warmup_start)}</span></div>`;
        } else {
          const pct = Math.round(b.days_done / 14 * 100);
          html += `<div style="margin-bottom:6px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:3px;"><span style="color:var(--purple);">&#9679; ${esc(b.total)} new accounts warming</span><span>Day ${esc(b.days_done)}/14</span></div><div style="background:var(--bg-input);border-radius:4px;height:5px;overflow:hidden;"><div style="background:var(--purple);height:100%;width:${pct}%;border-radius:4px;"></div></div></div>`;
        }
      }
      html += `</div>`;
    }
  }

  // ── Campaign assignment (acquisition groups) ──
  if (item.active_campaigns || item.paused_campaigns) {
    const active = item.active_campaigns || [];
    const paused = item.paused_campaigns || [];
    html += `<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px;font-size:12px;">`;
    if (item.campaign_conflict) {
      html += `<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:6px 10px;margin-bottom:6px;color:#dc2626;font-weight:600;">CONFLICT: ${active.length} active campaigns</div>`;
      // Conflict campaign rows are built below with event listeners
      html += `<div class="conflict-campaigns"></div>`;
    } else {
      html += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">`;
      html += `<span class="label" style="white-space:nowrap;">Campaign</span>`;
      const currentCampId = active.length === 1 ? active[0].id : '';
      html += `<select class="acq-campaign-select" data-group-id="${item.id}" data-group-name="${esc(item.name)}" data-current-camp="${currentCampId || 0}" style="flex:1;font-size:12px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-input);color:var(--text-primary);max-width:200px;cursor:pointer;">`;
      html += `<option value="">— Available —</option>`;
      html += `</select></div>`;
    }
    if (paused.length > 0) {
      html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;"><span class="label">Paused</span><span style="color:var(--text-muted);">${esc(paused.map(c => c.name).join(', '))}</span></div>`;
    }
    html += `</div>`;
  }

  // ── Footer dates ──
  const hasReady = item.ready_date && item.days_until_ready != null && item.days_until_ready > 0;
  const hasRotation = !!item.rotation_date;
  if (hasReady || hasRotation) {
    html += `<div class="cc-dates">`;
    if (hasReady) {
      const readyBadge = item.days_until_ready <= 0
        ? '<span class="badge badge-green">Ready</span>'
        : `<span class="badge badge-yellow">${esc(item.days_until_ready)}d left</span>`;
      html += `<div class="date-row"><span>Ready Date</span><span>${esc(item.ready_date)} ${readyBadge}</span></div>`;
    }
    if (hasRotation) {
      const rotBadge = item.days_until_rotation != null && item.days_until_rotation <= 7
        ? ' <span class="badge badge-red">Rotate soon</span>' : '';
      html += `<div class="date-row"><span>Rotation Date</span><span>${esc(item.rotation_date)}${rotBadge}</span></div>`;
    }
    html += `</div>`;
  }

  card.innerHTML = html;

  // ── Click to open detail ──
  card.addEventListener('click', (e) => {
    // Don't navigate if clicking a button or select
    if (e.target.closest('button') || e.target.closest('select')) return;
    location.hash = `client/${item.id}`;
  });

  // ── Wire up conflict campaign remove buttons ──
  if (item.campaign_conflict) {
    const conflictDiv = card.querySelector('.conflict-campaigns');
    if (conflictDiv) {
      const active = item.active_campaigns || [];
      active.forEach(c => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;';
        row.innerHTML = `<span style="color:#dc2626;">&#9679; ${esc(c.name)}</span>`;
        const removeBtn = document.createElement('button');
        removeBtn.style.cssText = 'font-size:10px;padding:2px 8px;border:1px solid #fecaca;border-radius:4px;background:#fef2f2;color:#dc2626;cursor:pointer;';
        removeBtn.textContent = 'Remove';
        removeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          unassignGroupCampaign(item.id, item.name, c.id, c.name);
        });
        row.appendChild(removeBtn);
        conflictDiv.appendChild(row);
      });
    }
  }

  // ── Wire up campaign select dropdown ──
  const select = card.querySelector('.acq-campaign-select');
  if (select) {
    select.addEventListener('click', (e) => e.stopPropagation());
    select.addEventListener('change', (e) => {
      e.stopPropagation();
      const currentCampId = parseInt(select.dataset.currentCamp) || 0;
      assignGroupCampaign(item.id, item.name, select.value, currentCampId);
    });
  }

  return card;
}

/* ─── Acquisition Alerts ─── */

function renderAcqAlerts(el, acqData) {
  const conflicts = acqData.campaign_conflicts || [];
  const empty = acqData.empty_campaigns || [];
  if (conflicts.length === 0 && empty.length === 0) return;

  if (conflicts.length > 0) {
    const banner = document.createElement('div');
    banner.className = 'alert-banner';
    banner.style.borderColor = '#fecaca';
    let html = '<h3 style="color:#dc2626;">Campaign Conflicts</h3>';
    conflicts.forEach(c => {
      html += `<div class="alert-item" style="color:#dc2626;">${esc(c.group)} is in ${c.campaigns.length} active campaigns: ${esc(c.campaigns.join(', '))}</div>`;
    });
    banner.innerHTML = html;
    el.appendChild(banner);
  }

  if (empty.length > 0) {
    const banner = document.createElement('div');
    banner.className = 'alert-banner';
    banner.style.borderColor = '#fed7aa';
    let html = '<h3 style="color:#ea580c;">Campaigns With No Inboxes</h3>';
    empty.forEach(c => {
      html += `<div class="alert-item" style="color:#ea580c;">${esc(c.name)} — active but has no email accounts assigned</div>`;
    });
    banner.innerHTML = html;
    el.appendChild(banner);
  }
}

/* ─── Campaign Dropdown Population ─── */

async function populateCampaignDropdowns(el, groups) {
  if (!acqCampaignsCache) {
    try {
      const data = await apiGet('/api/acquisition-campaigns');
      acqCampaignsCache = data.campaigns || [];
    } catch {
      return;
    }
  }

  const selects = (el || container).querySelectorAll('select.acq-campaign-select');
  selects.forEach(select => {
    const groupId = parseInt(select.dataset.groupId);
    const group = groups.find(g => g.id === groupId);
    const activeCampId = group?.active_campaigns?.length === 1 ? group.active_campaigns[0].id : null;

    acqCampaignsCache.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.name + (c.status === 'PAUSED' ? ' (paused)' : '');
      if (c.id === activeCampId) opt.selected = true;
      select.appendChild(opt);
    });
  });
}

/* ─── Campaign Assignment Actions ─── */

async function assignGroupCampaign(groupClientId, groupName, newCampId, currentCampId) {
  if (currentCampId && currentCampId !== parseInt(newCampId)) {
    if (!confirm(`Remove ${groupName} from current campaign before assigning to new one?`)) return;
    try {
      await apiPost('/api/acquisition/assign-campaign', { group_client_id: groupClientId, group_name: groupName, campaign_id: currentCampId, action: 'unassign' });
    } catch (e) {
      showToast('Failed to unassign: ' + e.message, 'error');
      return;
    }
  }
  if (newCampId) {
    const campName = acqCampaignsCache ? (acqCampaignsCache.find(c => c.id === parseInt(newCampId)) || {}).name || '' : '';
    if (!confirm(`Assign all ${groupName} accounts to "${campName}"?`)) {
      await reloadAcquisition();
      return;
    }
    try {
      const result = await apiPost('/api/acquisition/assign-campaign', { group_client_id: groupClientId, group_name: groupName, campaign_id: parseInt(newCampId), action: 'assign' });
      if (result.error) {
        showToast('Error: ' + result.error, 'error');
      } else {
        showToast(`Assigned ${groupName} to ${campName}`, 'success');
      }
    } catch (e) {
      showToast('Failed to assign: ' + e.message, 'error');
    }
  }
  await reloadAcquisition();
}

async function unassignGroupCampaign(groupClientId, groupName, campId, campName) {
  if (!confirm(`Remove ${groupName} from "${campName}"?`)) return;
  try {
    const result = await apiPost('/api/acquisition/assign-campaign', { group_client_id: groupClientId, group_name: groupName, campaign_id: campId, action: 'unassign' });
    if (result.error) {
      showToast('Error: ' + result.error, 'error');
    } else {
      showToast(`Removed ${groupName} from ${campName}`, 'success');
    }
  } catch (e) {
    showToast('Failed to unassign: ' + e.message, 'error');
  }
  await reloadAcquisition();
}

/* ─── Generic Groups Section ─── */

function renderGenericGroupsSection(el) {
  const genericData = store.get('genericGroups');
  if (!genericData?.groups?.length) return;

  const section = document.createElement('div');
  section.style.marginTop = '24px';

  const header = document.createElement('h2');
  header.className = 'section-title';
  header.textContent = 'Generic Groups';
  section.appendChild(header);

  // Stats row
  const ready = genericData.groups.filter(g => g.status === 'ready').length;
  const warming = genericData.groups.filter(g => g.status === 'warming').length;
  const statsRow = document.createElement('div');
  statsRow.className = 'summary-row';
  statsRow.appendChild(statCard({ value: genericData.total_accounts, label: 'Generic Inboxes' }));
  statsRow.appendChild(statCard({ value: ready, label: 'Ready for Clients', variant: 'good' }));
  if (warming > 0) {
    statsRow.appendChild(statCard({ value: warming, label: 'Still Warming', variant: 'warn' }));
  }
  statsRow.appendChild(statCard({ value: (genericData.total_daily_capacity || 0) + '/day', label: 'Total Capacity' }));
  section.appendChild(statsRow);

  // Group cards grid
  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  genericData.groups.forEach(g => grid.appendChild(buildGenericGroupCard(g)));
  section.appendChild(grid);

  el.appendChild(section);
}

function buildGenericGroupCard(g) {
  const card = document.createElement('div');
  card.className = 'client-card';
  card.style.cssText = 'position:relative;cursor:pointer;';

  const isReady = g.status === 'ready';
  const statusColor = isReady ? '#22c55e' : '#8b5cf6';
  const statusBg = isReady ? '#f0fdf4' : '#f5f3ff';
  const statusLabel = isReady ? 'Ready' : (g.days_left || 0) + 'd left';
  const progressPct = isReady ? 100 : Math.min(100, Math.round((g.days_warming / 14) * 100));

  let html = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="cc-name">${esc(g.name)}</span>
        <span class="badge" style="background:${statusBg};color:${statusColor};font-size:11px;padding:2px 8px;">${esc(statusLabel)}</span>
        <span class="cc-count">${esc(g.accounts)} accounts</span>
      </div>
    </div>
    <div class="cc-stats" style="grid-template-columns:1fr 1fr 1fr;">
      <div class="cc-stat"><span class="label">Domains</span><span>${esc(g.domains)}</span></div>
      <div class="cc-stat"><span class="label">Capacity</span><span>${esc(g.daily_capacity)}/day</span></div>
      <div class="cc-stat"><span class="label">Warmup Start</span><span>${esc(g.warmup_start || '—')}</span></div>
      <div class="cc-stat"><span class="label">${isReady ? 'Ready Since' : 'Ready Date'}</span><span>${esc(g.ready_date || '—')}</span></div>
      <div class="cc-stat"><span class="label">Health</span><span style="color:${g.health_score >= 85 ? '#22c55e' : g.health_score >= 60 ? '#f59e0b' : '#ef4444'}">${esc(g.health_score)}</span></div>
      <div class="cc-stat"><span class="label">SMTP Fail</span><span style="color:${g.smtp_failures > 0 ? '#ef4444' : '#22c55e'}">${esc(g.smtp_failures)}</span></div>
    </div>
    <div style="margin-top:10px;">
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:4px;">
        <span>Warmup Progress</span><span>${progressPct}%</span>
      </div>
      <div style="background:var(--bg-input);border-radius:4px;height:6px;overflow:hidden;">
        <div style="background:${isReady ? '#22c55e' : '#8b5cf6'};height:100%;width:${progressPct}%;border-radius:4px;transition:width 0.3s;"></div>
      </div>
    </div>
  `;

  card.innerHTML = html;

  // Assign button at bottom
  const assignBtn = document.createElement('button');
  assignBtn.style.cssText = `margin-top:12px;width:100%;background:${isReady ? 'var(--purple)' : 'var(--bg-raised)'};color:${isReady ? '#fff' : 'var(--text-secondary)'};border:1px solid ${isReady ? 'var(--purple)' : 'var(--border)'};padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:500;font-size:13px;`;
  assignBtn.textContent = 'Assign to Client';
  assignBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openAssignToClientModal(g.pipeline_id || '', g.name);
  });
  card.appendChild(assignBtn);

  // Card click navigates to detail
  card.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    location.hash = `client/${g.client_id}`;
  });

  return card;
}

/* ─── Assign to Client Modal ─── */

async function openAssignToClientModal(pipelineId, groupName) {
  const content = document.createElement('div');
  content.innerHTML = `
    <p style="color:var(--text-secondary);margin-bottom:16px;">Reassigning: <strong>${esc(groupName)}</strong></p>
    <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--text-muted);">Client</label>
    <select class="assign-client-select" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);margin-bottom:12px;">
      <option value="">Loading...</option>
    </select>
    <div class="new-client-row" style="display:none;margin-bottom:12px;">
      <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--text-muted);">New Client Name</label>
      <input type="text" class="new-client-name" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);" placeholder="Client name">
    </div>
    <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--text-muted);">Forwarding Domain</label>
    <input type="text" class="forwarding-input" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);margin-bottom:16px;" placeholder="example.com">
    <button class="assign-start-btn" disabled style="width:100%;padding:10px;border:none;border-radius:6px;background:var(--accent);color:var(--bg-root);font-weight:600;cursor:pointer;opacity:0.5;">Assign</button>
    <div class="assign-progress" style="display:none;margin-top:16px;"></div>
  `;

  openModal({ title: 'Assign to Client', content });

  const select = content.querySelector('.assign-client-select');
  const newClientRow = content.querySelector('.new-client-row');
  const newClientName = content.querySelector('.new-client-name');
  const fwdInput = content.querySelector('.forwarding-input');
  const startBtn = content.querySelector('.assign-start-btn');

  // Load client list
  try {
    const data = await apiGet('/api/clients/list');
    const clients = data.clients || [];
    select.innerHTML = '<option value="">Select a client...</option>';
    clients.forEach(c => { select.innerHTML += `<option value="${esc(c)}">${esc(c)}</option>`; });
    select.innerHTML += '<option value="__new__">+ Add New Client</option>';
  } catch {
    select.innerHTML = '<option value="">Error loading clients</option>';
  }

  function checkReady() {
    const selectVal = select.value;
    const hasClient = selectVal === '__new__' ? newClientName.value.trim().length > 0 : selectVal.length > 0;
    const hasFwd = fwdInput.value.trim().length > 0;
    startBtn.disabled = !(hasClient && hasFwd);
    startBtn.style.opacity = startBtn.disabled ? '0.5' : '1';
  }

  select.addEventListener('change', () => {
    newClientRow.style.display = select.value === '__new__' ? 'block' : 'none';
    if (select.value !== '__new__') newClientName.value = '';
    checkReady();
  });
  newClientName.addEventListener('input', checkReady);
  fwdInput.addEventListener('input', checkReady);

  startBtn.addEventListener('click', async () => {
    const isNew = select.value === '__new__';
    const clientName = isNew ? newClientName.value.trim() : select.value;
    const fwd = fwdInput.value.trim();

    startBtn.disabled = true;
    startBtn.textContent = 'Assigning...';

    try {
      const resp = await fetch('/api/pipeline/assign-client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipeline_id: pipelineId, client_name: clientName, forwarding_domain: fwd, is_new_client: isNew }),
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const progressDiv = content.querySelector('.assign-progress');
      progressDiv.style.display = 'block';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.status === 'complete') {
                progressDiv.innerHTML = '<div style="color:var(--accent);font-weight:600;">Assignment complete!</div>';
                showToast(`Assigned ${groupName} to ${clientName}`, 'success');
                setTimeout(() => { closeModal(); load(); }, 1500);
                return;
              }
              if (evt.status === 'error') {
                progressDiv.innerHTML = `<div style="color:var(--red);">${esc(evt.message)}</div>`;
                startBtn.textContent = 'Assign';
                startBtn.disabled = false;
                return;
              }
              if (evt.message) {
                progressDiv.innerHTML += `<div style="font-size:13px;color:var(--text-secondary);padding:2px 0;">${esc(evt.message)}</div>`;
              }
            } catch { /* skip non-JSON */ }
          }
        }
      }
    } catch (e) {
      showToast('Assignment failed: ' + e.message, 'error');
      startBtn.textContent = 'Assign';
      startBtn.disabled = false;
    }
  });
}

/* ─── A/B Rotation Section ─── */

function renderRotationSection(el) {
  const rotationData = store.get('rotationStatus');
  if (!rotationData?.rotations?.length) return;

  const section = document.createElement('div');
  section.style.marginTop = '24px';

  // Header with swap-all button
  const headerRow = document.createElement('div');
  headerRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;';
  const header = document.createElement('h2');
  header.className = 'section-title';
  header.textContent = 'A/B Rotation';
  header.style.margin = '0';
  headerRow.appendChild(header);

  const swapAllBtn = document.createElement('button');
  swapAllBtn.className = 'action-btn secondary';
  swapAllBtn.style.fontSize = '12px';
  swapAllBtn.textContent = 'Swap All Clients';
  swapAllBtn.addEventListener('click', async () => {
    if (!confirm('Swap ALL clients to their other group? This affects all campaigns.')) return;
    swapAllBtn.disabled = true;
    swapAllBtn.textContent = 'Swapping All...';
    try {
      const result = await apiPost('/api/rotation/swap-all', {});
      const ok = (result.results || []).filter(r => r.ok).length;
      const fail = (result.results || []).filter(r => r.error).length;
      showToast(`Swap All complete: ${ok} succeeded, ${fail} failed.`, ok > 0 ? 'success' : 'error');
      await load();
    } catch (e) {
      showToast('Swap All error: ' + e.message, 'error');
    }
    swapAllBtn.disabled = false;
    swapAllBtn.textContent = 'Swap All Clients';
  });
  headerRow.appendChild(swapAllBtn);
  section.appendChild(headerRow);

  // Rotation cards
  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  rotationData.rotations.forEach(rot => {
    const card = document.createElement('div');
    card.className = 'client-card';
    const aCount = (rot.group_a_ids || []).length;
    const bCount = (rot.group_b_ids || []).length;
    const active = rot.active_group || 'A';
    const lastSwap = rot.last_swap_date || 'Never';
    const aBadge = active === 'A' ? 'badge-green' : 'badge-muted';
    const bBadge = active === 'B' ? 'badge-green' : 'badge-muted';

    card.innerHTML = `
      <div class="client-header">
        <h3 class="client-name">${esc(rot.client_name)}</h3>
        <span class="badge ${active === 'A' ? 'badge-green' : 'badge-blue'}">Group ${esc(active)} Active</span>
      </div>
      <div class="client-stats">
        <div class="stat"><span class="stat-value ${aBadge}">${aCount}</span><span class="stat-label">Group A</span></div>
        <div class="stat"><span class="stat-value ${bBadge}">${bCount}</span><span class="stat-label">Group B</span></div>
        <div class="stat"><span class="stat-value">${esc(lastSwap)}</span><span class="stat-label">Last Swap</span></div>
      </div>
      <div style="margin-top:8px;text-align:right;"></div>
    `;

    const swapBtn = document.createElement('button');
    swapBtn.className = 'action-btn secondary';
    swapBtn.style.fontSize = '11px';
    swapBtn.textContent = `Swap to Group ${active === 'A' ? 'B' : 'A'}`;
    swapBtn.addEventListener('click', async () => {
      if (!confirm(`Swap ${rot.client_name} to the other group? This will update all their campaigns.`)) return;
      swapBtn.disabled = true;
      swapBtn.textContent = 'Swapping...';
      try {
        const result = await apiPost('/api/rotation/swap', { client_name: rot.client_name });
        if (result.ok) {
          showToast(`Swapped ${rot.client_name} to Group ${result.new_group}. ${result.campaigns_updated} campaigns updated.`, 'success');
          await load();
        } else {
          showToast('Swap failed: ' + (result.error || 'Unknown error'), 'error');
        }
      } catch (e) {
        showToast('Swap error: ' + e.message, 'error');
      }
      swapBtn.disabled = false;
    });
    card.querySelector('div:last-child').appendChild(swapBtn);
    grid.appendChild(card);
  });

  section.appendChild(grid);
  el.appendChild(section);
}

/* ─── Unassigned Accounts Section ─── */

function renderUnassignedSection(el, overviewData) {
  const unassignedData = store.get('unassigned');
  if (!unassignedData || !unassignedData.count || unassignedData.count === 0) return;

  const section = document.createElement('div');
  section.style.marginTop = '24px';

  const header = document.createElement('h2');
  header.className = 'section-title';
  header.textContent = `Unassigned Accounts (${unassignedData.count})`;
  section.appendChild(header);

  // Assignment controls row
  const controlsRow = document.createElement('div');
  controlsRow.style.cssText = 'display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;';
  controlsRow.innerHTML = `
    <select class="assign-client-dropdown" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);font-size:13px;">
      <option value="">-- Assign to client --</option>
    </select>
    <button class="assign-selected-btn" disabled style="padding:6px 14px;border:none;border-radius:6px;background:var(--accent);color:var(--bg-root);font-weight:600;font-size:13px;cursor:pointer;opacity:0.5;">Assign Selected</button>
    <span class="assign-status-text" style="font-size:13px;color:var(--text-muted);"></span>
  `;
  section.appendChild(controlsRow);

  // Populate client dropdown
  const clientSelect = controlsRow.querySelector('.assign-client-dropdown');
  (overviewData.clients || []).forEach(cl => {
    clientSelect.innerHTML += `<option value="${cl.id}">${esc(cl.name)}</option>`;
  });

  // Table with checkboxes
  const accounts = unassignedData.accounts || [];
  const tableWrap = document.createElement('div');
  tableWrap.style.cssText = 'overflow-x:auto;';

  const table = dataTable({
    columns: [
      {
        key: '_check',
        label: '',
        width: '40px',
        render: (row) => {
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.className = 'ua-check';
          cb.value = row.id;
          cb.addEventListener('change', updateAssignState);
          return cb;
        },
      },
      { key: 'email', label: 'Email' },
      { key: 'domain', label: 'Domain' },
      {
        key: 'warmup_status',
        label: 'Warmup',
        render: (row) => `<span style="color:${row.warmup_status === 'ACTIVE' ? '#22c55e' : '#f59e0b'}">${esc(row.warmup_status)}</span>`,
      },
      { key: 'warmup_reputation', label: 'Reputation' },
      {
        key: 'smtp_ok',
        label: 'SMTP',
        render: (row) => `<span style="color:${row.smtp_ok ? '#22c55e' : '#ef4444'}">${row.smtp_ok ? 'OK' : 'FAIL'}</span>`,
      },
    ],
    rows: accounts,
    emptyMessage: 'No unassigned accounts',
  });

  // Add select-all checkbox to header
  const headerCheckbox = document.createElement('input');
  headerCheckbox.type = 'checkbox';
  headerCheckbox.addEventListener('change', () => {
    table.querySelectorAll('.ua-check').forEach(cb => { cb.checked = headerCheckbox.checked; });
    updateAssignState();
  });
  const firstTh = table.querySelector('th');
  if (firstTh) {
    firstTh.textContent = '';
    firstTh.appendChild(headerCheckbox);
  }

  tableWrap.appendChild(table);
  section.appendChild(tableWrap);

  const assignBtn = controlsRow.querySelector('.assign-selected-btn');
  const statusText = controlsRow.querySelector('.assign-status-text');

  function updateAssignState() {
    const selected = section.querySelectorAll('.ua-check:checked').length;
    const clientSelected = clientSelect.value;
    const ready = selected > 0 && clientSelected;
    assignBtn.disabled = !ready;
    assignBtn.style.opacity = ready ? '1' : '0.5';
  }

  clientSelect.addEventListener('change', updateAssignState);

  assignBtn.addEventListener('click', async () => {
    const accountIds = Array.from(section.querySelectorAll('.ua-check:checked')).map(cb => parseInt(cb.value));
    const clientId = parseInt(clientSelect.value);
    if (!accountIds.length || !clientId) return;

    assignBtn.disabled = true;
    statusText.textContent = `Assigning ${accountIds.length} accounts...`;
    try {
      const result = await apiPost('/api/assign', { account_ids: accountIds, client_id: clientId });
      statusText.textContent = `Done! ${result.success} assigned, ${result.fail} failed.`;
      showToast(`Assigned ${result.success} accounts`, 'success');
      setTimeout(() => load(), 2000);
    } catch (e) {
      statusText.textContent = 'Error: ' + e.message;
      showToast('Assignment error: ' + e.message, 'error');
    }
  });

  el.appendChild(section);
}

/* ─── Setup Pipelines Section ─── */

const STEP_LABELS = {
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

function renderSetupPipelineSteps(steps) {
  return steps.map((s, i) => {
    const cls = s.status || 'pending';
    const connector = i < steps.length - 1
      ? `<div class="pill-connector ${s.status === 'completed' ? 'done' : 'pending'}"></div>`
      : '';
    const shortName = s.name
      .replace('Connect Domains', 'Connect')
      .replace('Create Inboxes', 'Inboxes')
      .replace('Profile Photos', 'Photos')
      .replace('SmartLead Export', 'Export')
      .replace('Tag & Assign', 'Tag')
      .replace('Enable Warmup', 'Warmup');
    return `<span class="pill-step ${cls}"><span class="pill-icon">${stepStatusIcon(s.status)}</span>${esc(shortName)}</span>${connector}`;
  }).join('');
}

function setupPipelineStatusLine(p) {
  if (p.status === 'completed') return 'Complete';
  if (p.status === 'failed') {
    const failed = p.steps.find(s => s.status === 'failed');
    return failed ? 'Failed: ' + (failed.error || failed.name) : 'Failed';
  }
  const running = p.steps.find(s => s.status === 'running');
  if (running) return running.name + '... ' + running.progress + '/' + running.total;
  return p.status;
}

function renderSetupPipelinesSection(el) {
  const pipelineData = store.get('setupPipelines');
  if (!pipelineData?.pipelines?.length) return;

  const pipelines = pipelineData.pipelines;

  const section = document.createElement('div');
  section.style.marginTop = '24px';

  const header = document.createElement('h2');
  header.className = 'section-title';
  header.textContent = 'Setup Pipelines';
  section.appendChild(header);

  const grid = document.createElement('div');
  grid.className = 'clients-grid';

  pipelines.forEach(p => {
    const card = document.createElement('div');
    card.className = 'client-card';
    card.style.cursor = 'pointer';

    const statusLine = setupPipelineStatusLine(p);
    const statusBg = p.status === 'completed' ? 'var(--accent-bg)' : p.status === 'failed' ? '#fef2f2' : 'var(--accent-bg)';
    const statusColor = p.status === 'completed' ? 'var(--accent)' : p.status === 'failed' ? 'var(--red)' : 'var(--accent)';

    card.innerHTML = `
      <div class="cc-header">
        <span class="cc-name">${esc(p.name)}</span>
        <span class="badge" style="background:${statusBg};color:${statusColor};">${esc(p.type)}</span>
      </div>
      <div class="pill-stepper">${renderSetupPipelineSteps(p.steps)}</div>
      <div class="pipeline-status-line">${esc(statusLine)}</div>
    `;

    if (p.status === 'failed') {
      const retryBtn = document.createElement('button');
      retryBtn.style.cssText = 'margin-top:8px;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;';
      retryBtn.textContent = 'Retry';
      retryBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await apiPost('/api/setup-pipeline/retry', { pipeline_id: p.id });
        await fetchSlice('setupPipelines', '/api/setup-pipelines').catch(() => null);
      });
      card.appendChild(retryBtn);
    }

    card.addEventListener('click', () => showSetupPipelineDetail(p.id));
    grid.appendChild(card);
  });

  section.appendChild(grid);
  el.appendChild(section);

  // Auto-poll if running
  const hasRunning = pipelines.some(p => p.status === 'running');
  if (hasRunning && !setupPipelinePollInterval) {
    setupPipelinePollInterval = setInterval(async () => {
      await fetchSlice('setupPipelines', '/api/setup-pipelines').catch(() => null);
      const updated = store.get('setupPipelines');
      const stillRunning = (updated?.pipelines || []).some(p => p.status === 'running');
      if (!stillRunning && setupPipelinePollInterval) {
        clearInterval(setupPipelinePollInterval);
        setupPipelinePollInterval = null;
      }
    }, 5000);
  } else if (!hasRunning && setupPipelinePollInterval) {
    clearInterval(setupPipelinePollInterval);
    setupPipelinePollInterval = null;
  }
}

async function showSetupPipelineDetail(id) {
  try {
    const p = await apiGet('/api/setup-pipeline/' + id);
    if (p.error) return;

    const content = document.createElement('div');
    content.innerHTML = `<div class="pill-stepper" style="margin-bottom:16px;">${renderSetupPipelineSteps(p.steps)}</div>`;

    p.steps.forEach(s => {
      const timing = s.completed_at && s.started_at
        ? Math.round((new Date(s.completed_at) - new Date(s.started_at)) / 1000) + 's'
        : s.status === 'running' ? 'running...' : '';

      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light);';

      const iconColor = s.status === 'completed' || s.status === 'running' ? 'var(--accent)' : s.status === 'failed' ? 'var(--red)' : 'var(--text-muted)';
      row.innerHTML = `
        <span style="color:${iconColor};font-size:14px;width:20px;text-align:center;">${stepStatusIcon(s.status)}</span>
        <span style="flex:1;font-size:13px;color:var(--text-primary);">${esc(s.name)}</span>
        <span style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">${esc(s.progress)}/${esc(s.total)}</span>
        <span style="font-size:11px;color:var(--text-muted);width:60px;text-align:right;">${esc(timing)}</span>
      `;
      content.appendChild(row);

      if (s.error) {
        const errDiv = document.createElement('div');
        errDiv.style.cssText = 'color:var(--red);font-size:11px;margin-top:4px;';
        errDiv.textContent = s.error;
        content.appendChild(errDiv);
      }

      if (s.status === 'failed') {
        const retryBtn = document.createElement('button');
        retryBtn.style.cssText = 'margin-top:4px;font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;';
        retryBtn.textContent = 'Retry';
        retryBtn.addEventListener('click', async () => {
          await apiPost('/api/setup-pipeline/retry', { pipeline_id: p.id });
          closeModal();
          await fetchSlice('setupPipelines', '/api/setup-pipelines').catch(() => null);
        });
        content.appendChild(retryBtn);
      }
    });

    const titleText = `${p.name} ${p.type}`;
    openModal({ title: titleText, content });
  } catch { /* silent */ }
}

/* ─── Generic Setup Tracker ─── */

const GENERIC_STEP_LABELS = {
  wait_active: 'Mailboxes Active',
  smartlead_export: 'SmartLead Export',
  smartlead_verify: 'Verify Accounts',
  tag_assign: 'Tag & Assign',
  enable_warmup: 'Enable Warmup',
  complete: 'Complete',
};

const GENERIC_STEP_ORDER = ['wait_active', 'smartlead_export', 'smartlead_verify', 'tag_assign', 'enable_warmup', 'complete'];

const GENERIC_COMPLETED_MAP = {
  mailboxes_active: 'wait_active',
  smartlead_export: 'smartlead_export',
  smartlead_verified: 'smartlead_verify',
  tagged: 'tag_assign',
  warmup_enabled: 'enable_warmup',
};

function renderGenericSetupTracker(el) {
  if (!genericSetupData) return;
  if (!genericSetupData.running && genericSetupData.step === 'unknown') {
    if (genericTrackerInterval) { clearInterval(genericTrackerInterval); genericTrackerInterval = null; }
    return;
  }

  const section = document.createElement('div');
  section.style.marginTop = '24px';

  const header = document.createElement('h2');
  header.className = 'section-title';
  header.textContent = 'Generic Setup Tracker';
  section.appendChild(header);

  const completedSteps = (genericSetupData.completed_steps || [])
    .map(s => GENERIC_COMPLETED_MAP[s])
    .filter(Boolean);
  const currentStep = genericSetupData.step || 'unknown';
  const progress = Math.round((genericSetupData.progress || 0) * 100);
  const isComplete = genericSetupData.step === 'complete';
  const barPct = isComplete ? 100 : progress;
  const detail = genericSetupData.detail || '';
  const updatedAt = genericSetupData.updated_at ? new Date(genericSetupData.updated_at).toLocaleTimeString() : '';

  let stepsHtml = GENERIC_STEP_ORDER.map(step => {
    let cls = '';
    if (completedSteps.includes(step) || (step === 'complete' && isComplete)) cls = 'done';
    else if (step === currentStep) cls = 'active';
    const icon = cls === 'done' ? '✓' : (cls === 'active' ? '●' : '○');
    return `<span class="generic-tracker-step ${cls}">${icon} ${esc(GENERIC_STEP_LABELS[step])}</span>`;
  }).join('');

  const card = document.createElement('div');
  card.className = 'generic-tracker-card';
  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-weight:600;font-family:var(--font-display);font-size:14px;">
        Generic F / G / H / I
        ${isComplete ? ' <span style="color:var(--accent);">✔ Complete</span>' : ''}
      </span>
      <span style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);">
        ${updatedAt ? 'Updated ' + esc(updatedAt) : ''}
      </span>
    </div>
    <div class="generic-tracker-steps">${stepsHtml}</div>
    ${isComplete ? '' : `
      <div class="generic-tracker-bar"><div class="generic-tracker-bar-fill" style="width:${barPct}%;"></div></div>
      <div class="generic-tracker-detail">${esc(detail)}${barPct > 0 ? ' (' + barPct + '%)' : ''}</div>
    `}
  `;
  section.appendChild(card);
  el.appendChild(section);

  // Auto-poll if running
  if (genericSetupData.running && !genericTrackerInterval) {
    genericTrackerInterval = setInterval(async () => {
      await loadGenericSetupStatus();
      render();
    }, 10000);
  } else if (!genericSetupData.running && genericTrackerInterval) {
    clearInterval(genericTrackerInterval);
    genericTrackerInterval = null;
  }
}
