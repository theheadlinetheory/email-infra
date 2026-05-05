/**
 * Pipelines view — setup pipelines (new format) + legacy pipelines.
 * Features: pipeline cards with step pills, retry/skip, domain error tables,
 * pending removals, assign-to-client modal (SSE), creation forms.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiGet, apiPost, connectSSE } from '../core/api.js';
import { openModal, closeModal } from '../components/modal.js';
import { showToast } from '../components/toast.js';
import { sseProgress } from '../components/sse-progress.js';

// ── Constants ──

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

const ASSIGN_STEPS = [
  { id: 1, label: 'Validate pipeline & domains' },
  { id: 2, label: 'Update forwarding addresses' },
  { id: 3, label: 'Rename ZapMail tags' },
  { id: 4, label: 'Update SmartLead tags' },
  { id: 5, label: 'Assign to campaigns' },
  { id: 6, label: 'Update dashboard records' },
  { id: 7, label: 'Finalize' },
];

// ── Module state ──

let container = null;
let unsubs = [];
let pipelinePollingInterval = null;
let setupPipelinePollInterval = null;
let assignmentInProgress = false;

// ── Lifecycle ──

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('pipelines', render));
  unsubs.push(store.subscribe('setupPipelines', render));
  unsubs.push(store.subscribe('loading', render));
  unsubs.push(store.subscribe('errors', render));
  loadAll();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  container = null;
  stopPipelinePolling();
  stopSetupPolling();
}

// ── Data loading ──

async function loadAll() {
  await Promise.all([loadLegacyPipelines(), loadSetupPipelines()]);
}

async function loadLegacyPipelines() {
  try {
    const data = await fetchSlice('pipelines', '/api/pipeline/active');
    const hasRunning = (data?.pipelines || []).some(p => p.status === 'running');
    if (hasRunning) startPipelinePolling();
    else stopPipelinePolling();
  } catch (e) { /* handled by store */ }
}

async function loadSetupPipelines() {
  try {
    const data = await fetchSlice('setupPipelines', '/api/setup-pipelines');
    const pipelines = data?.pipelines || [];
    const hasRunning = pipelines.some(p => p.status === 'running');
    if (hasRunning) startSetupPolling();
    else stopSetupPolling();
  } catch (e) { /* handled by store */ }
}

// ── Polling ──

function startPipelinePolling() {
  if (pipelinePollingInterval) return;
  pipelinePollingInterval = setInterval(async () => {
    try {
      const data = await apiGet('/api/pipeline/active');
      store.setData('pipelines', data);
      const hasRunning = (data?.pipelines || []).some(p => p.status === 'running');
      if (!hasRunning) stopPipelinePolling();
    } catch (e) { /* silent */ }
  }, 10000);
}

function stopPipelinePolling() {
  if (pipelinePollingInterval) {
    clearInterval(pipelinePollingInterval);
    pipelinePollingInterval = null;
  }
}

function startSetupPolling() {
  if (setupPipelinePollInterval) return;
  setupPipelinePollInterval = setInterval(async () => {
    try {
      const data = await apiGet('/api/setup-pipelines');
      store.setData('setupPipelines', data);
      const hasRunning = (data?.pipelines || []).some(p => p.status === 'running');
      if (!hasRunning) stopSetupPolling();
    } catch (e) { /* silent */ }
  }, 5000);
}

function stopSetupPolling() {
  if (setupPipelinePollInterval) {
    clearInterval(setupPipelinePollInterval);
    setupPipelinePollInterval = null;
  }
}

// ── Render ──

function render() {
  if (!container) return;

  const pipelineData = store.get('pipelines');
  const setupData = store.get('setupPipelines');
  const loading = store.get('loading');
  const errors = store.get('errors');

  const isLoading = (loading?.pipelines || loading?.setupPipelines) && !pipelineData && !setupData;
  const hasError = (errors?.pipelines || errors?.setupPipelines) && !pipelineData && !setupData;

  if (isLoading) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading pipelines...</div>';
    return;
  }

  if (hasError) {
    const errMsg = errors?.pipelines || errors?.setupPipelines || 'Unknown error';
    container.innerHTML = `<div class="error-card"><div class="error-msg">${esc(errMsg)}</div></div>`;
    const retryBtn = document.createElement('button');
    retryBtn.className = 'retry-btn';
    retryBtn.textContent = 'Retry';
    retryBtn.addEventListener('click', loadAll);
    container.querySelector('.error-card')?.appendChild(retryBtn);
    return;
  }

  const setupPipelines = setupData?.pipelines || [];
  const legacyPipelines = pipelineData?.pipelines || [];
  const activeCount = [...setupPipelines, ...legacyPipelines].filter(
    p => p.status === 'running' || p.status === 'awaiting_removal'
  ).length;

  container.innerHTML = '';

  // Page header with New Pipeline button
  const header = document.createElement('div');
  header.className = 'page-header';
  header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;';
  header.innerHTML = `
    <div>
      <h2>Pipelines</h2>
      <p class="subtitle">${activeCount > 0 ? activeCount + ' active' : 'No active pipelines'}</p>
    </div>
  `;

  const newBtn = document.createElement('button');
  newBtn.style.cssText = 'background:var(--accent);color:var(--bg-root);border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;';
  newBtn.textContent = '+ New Pipeline';
  newBtn.addEventListener('click', openNewPipelineModal);
  header.appendChild(newBtn);
  container.appendChild(header);

  // Setup pipelines section
  if (setupPipelines.length > 0) {
    const section = document.createElement('div');
    section.innerHTML = '<h3 style="font-size:15px;font-weight:600;margin-bottom:12px;color:var(--text-primary);">Setup Pipelines</h3>';
    const grid = document.createElement('div');
    grid.className = 'clients-grid';
    setupPipelines.forEach(p => grid.appendChild(setupPipelineCard(p)));
    section.appendChild(grid);
    container.appendChild(section);
  }

  // Legacy pipelines section
  if (legacyPipelines.length > 0) {
    const section = document.createElement('div');
    section.style.marginTop = '24px';
    section.innerHTML = '<h3 style="font-size:15px;font-weight:600;margin-bottom:12px;color:var(--text-primary);">Infrastructure Pipelines</h3>';
    legacyPipelines.forEach(p => section.appendChild(legacyPipelineCard(p)));
    container.appendChild(section);
  }

  // Empty state
  if (setupPipelines.length === 0 && legacyPipelines.length === 0) {
    const empty = document.createElement('div');
    empty.style.cssText = 'text-align:center;color:var(--text-muted);padding:40px;';
    empty.textContent = 'No pipelines yet. Click "+ New Pipeline" to start one.';
    container.appendChild(empty);
  }
}

// ── Setup pipeline card ──

function setupPipelineCard(p) {
  const card = document.createElement('div');
  card.className = 'client-card';
  card.style.cursor = 'pointer';

  const statusColor = p.status === 'completed' ? 'var(--accent)' :
                      p.status === 'failed' ? 'var(--red)' : 'var(--accent)';
  const badgeBg = p.status === 'completed' ? 'var(--accent-bg)' :
                  p.status === 'failed' ? '#fef2f2' : 'var(--accent-bg)';

  const statusLine = setupPipelineStatusLine(p);

  card.innerHTML = `
    <div class="cc-header">
      <span class="cc-name">${esc(p.name)}</span>
      <span class="badge" style="background:${badgeBg};color:${statusColor};">${esc(p.type)}</span>
    </div>
    <div class="pill-stepper">${renderSetupStepPills(p.steps || [])}</div>
    <div class="pipeline-status-line" style="font-size:12px;color:var(--text-muted);margin-top:8px;">${esc(statusLine)}</div>
  `;

  if (p.status === 'failed') {
    const retryBtn = document.createElement('button');
    retryBtn.style.cssText = 'margin-top:8px;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;';
    retryBtn.textContent = 'Retry';
    retryBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await retrySetup(p.id);
    });
    card.appendChild(retryBtn);
  }

  card.addEventListener('click', () => showSetupPipelineDetail(p.id));
  return card;
}

function setupPipelineStatusLine(p) {
  if (p.status === 'completed') return 'Complete';
  if (p.status === 'failed') {
    const failed = (p.steps || []).find(s => s.status === 'failed');
    return failed ? 'Failed: ' + (failed.error || failed.name) : 'Failed';
  }
  const running = (p.steps || []).find(s => s.status === 'running');
  if (running) return running.name + '... ' + running.progress + '/' + running.total;
  return p.status || 'Pending';
}

function renderSetupStepPills(steps) {
  return steps.map((s, i) => {
    const cls = s.status || 'pending';
    const icon = stepStatusIcon(s.status);
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
    return `<span class="pill-step ${cls}"><span class="pill-icon">${icon}</span>${esc(shortName)}</span>${connector}`;
  }).join('');
}

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

// ── Setup pipeline detail modal ──

async function showSetupPipelineDetail(id) {
  try {
    const p = await apiGet('/api/setup-pipeline/' + id);
    if (p.error) { showToast(p.error, 'error'); return; }

    const content = document.createElement('div');

    // Step pills at top
    const stepper = document.createElement('div');
    stepper.className = 'pill-stepper';
    stepper.style.marginBottom = '16px';
    stepper.innerHTML = renderSetupStepPills(p.steps || []);
    content.appendChild(stepper);

    // Detailed step list
    (p.steps || []).forEach(s => {
      const icon = stepStatusIcon(s.status);
      const color = stepStatusColor(s.status);
      const timing = (s.completed_at && s.started_at)
        ? Math.round((new Date(s.completed_at) - new Date(s.started_at)) / 1000) + 's'
        : s.status === 'running' ? 'running...' : '';

      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light);';
      row.innerHTML = `
        <span style="color:${color};font-size:14px;width:20px;text-align:center;">${icon}</span>
        <span style="flex:1;font-size:13px;color:var(--text-primary);">${esc(s.name)}</span>
        <span style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">${s.progress}/${s.total}</span>
        <span style="font-size:11px;color:var(--text-muted);width:60px;text-align:right;">${timing}</span>
      `;
      content.appendChild(row);

      if (s.error) {
        const errEl = document.createElement('div');
        errEl.style.cssText = 'color:var(--red);font-size:11px;margin-top:4px;padding-left:30px;';
        errEl.textContent = s.error;
        content.appendChild(errEl);
      }

      if (s.status === 'failed') {
        const retryBtn = document.createElement('button');
        retryBtn.style.cssText = 'margin-top:4px;margin-left:30px;font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;';
        retryBtn.textContent = 'Retry';
        retryBtn.addEventListener('click', async () => {
          await retrySetup(p.id);
          closeModal();
        });
        content.appendChild(retryBtn);
      }
    });

    const typeLabel = p.type || '';
    openModal({
      title: p.name + (typeLabel ? ' — ' + typeLabel : ''),
      content,
    });
  } catch (e) {
    showToast('Failed to load pipeline details: ' + e.message, 'error');
  }
}

async function retrySetup(id) {
  try {
    await apiPost('/api/setup-pipeline/retry', { pipeline_id: id });
    showToast('Retrying pipeline...', 'info');
    await loadSetupPipelines();
  } catch (e) {
    showToast('Retry failed: ' + e.message, 'error');
  }
}

// ── Legacy pipeline card ──

function legacyPipelineCard(p) {
  const card = document.createElement('div');
  card.style.cssText = 'background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:12px;';

  const statusColor = pipelineStatusColor(p.status);
  const statusLabel = pipelineStatusLabel(p.status);
  const typeLabel = pipelineTypeLabel(p.type);

  let html = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <div>
        <span style="font-size:16px;font-weight:600;">${esc(p.client_name)}</span>
        <span style="font-size:13px;color:var(--text-muted);margin-left:12px;">${esc(typeLabel)}</span>
      </div>
      <span style="color:${statusColor};font-weight:500;">${esc(statusLabel)}</span>
    </div>
    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px;">Domains: ${(p.domains || []).length}</div>
    <div style="font-size:12px;color:var(--text-muted);">Started: ${p.created_at ? new Date(p.created_at).toLocaleString() : '—'}</div>
  `;

  card.innerHTML = html;

  // Step pills
  if (p.status !== 'complete') {
    const pillsEl = document.createElement('div');
    pillsEl.innerHTML = renderLegacyStepPills(p);
    card.appendChild(pillsEl);
  }

  // Domain detail table on error
  const dd = p.domain_details || {};
  let hasErrors = false;
  for (const dk in dd) { if (dd[dk].step_status === 'error') { hasErrors = true; break; } }
  if ((p.status === 'error' || hasErrors) && Object.keys(dd).length > 0) {
    const tableEl = document.createElement('div');
    tableEl.innerHTML = renderDomainDetailTable(dd);
    card.appendChild(tableEl);
  }

  // Error action buttons
  if (p.status === 'error') {
    card.appendChild(renderPipelineErrorActions(p, dd));
  }

  // Pending removals
  if (p.status === 'awaiting_removal' && p.pending_removals) {
    card.appendChild(renderPendingRemovals(p));
  }

  // Assign to client (generic groups only)
  const isGeneric = p.client_name && p.client_name.toLowerCase().startsWith('generic');
  if (isGeneric && (p.status === 'complete' || p.status === 'running')) {
    const assignWrap = document.createElement('div');
    assignWrap.style.cssText = 'margin-top:12px;display:flex;justify-content:flex-end;';
    const assignBtn = document.createElement('button');
    assignBtn.style.cssText = 'background:var(--purple);color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:500;font-size:13px;';
    assignBtn.textContent = 'Assign to Client';
    assignBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openAssignModal(p.id, p.client_name);
    });
    assignWrap.appendChild(assignBtn);
    card.appendChild(assignWrap);
  }

  // Inline errors
  if (p.errors && p.errors.length > 0) {
    const errWrap = document.createElement('div');
    errWrap.style.cssText = 'margin-top:8px;font-size:12px;color:var(--red);';
    p.errors.forEach(e => {
      const row = document.createElement('div');
      row.textContent = e;
      errWrap.appendChild(row);
    });
    card.appendChild(errWrap);
  }

  return card;
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
  return (status || '').charAt(0).toUpperCase() + (status || '').slice(1);
}

function renderLegacyStepPills(p) {
  const allSteps = p.steps || [];
  const currentIdx = allSteps.indexOf(p.current_step);
  const stepSuffix = (p.retry_info && p.status === 'running')
    ? ' (attempt ' + p.retry_info.attempt + '/' + p.retry_info.max_attempts + ')'
    : '';

  const pills = allSteps.map((s, i) => {
    let color, textColor;
    if (i < currentIdx) { color = '#22c55e'; textColor = '#fff'; }
    else if (i === currentIdx) { color = p.status === 'error' ? '#ef4444' : '#8b5cf6'; textColor = '#fff'; }
    else { color = '#333'; textColor = '#666'; }
    const label = (STEP_LABELS[s] || s) + (i === currentIdx ? stepSuffix : '');
    return `<div style="background:${color};padding:4px 10px;border-radius:4px;font-size:11px;color:${textColor};" title="${esc(label)}">${esc(label)}</div>`;
  }).join('');

  return `<div style="display:flex;gap:4px;margin-top:12px;flex-wrap:wrap;">${pills}</div>`;
}

// ── Domain detail table ──

function renderDomainDetailTable(dd) {
  const thStyle = 'text-align:left;padding:6px 8px;color:var(--text-muted);border-bottom:1px solid var(--border);';
  const tdStyle = 'padding:6px 8px;border-bottom:1px solid var(--border-light);';

  let html = '<div style="margin-top:12px;background:var(--bg-input);border-radius:8px;padding:12px;overflow-x:auto;">';
  html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">';
  html += '<thead><tr>';
  html += `<th style="${thStyle}">Domain</th>`;
  html += `<th style="${thStyle}">Status</th>`;
  html += `<th style="${thStyle}">Error</th>`;
  html += `<th style="${thStyle}">Attempts</th>`;
  html += '</tr></thead><tbody>';

  for (const domain in dd) {
    const detail = dd[domain];
    const errorText = detail.error || '—';
    const attemptText = detail.step_status === 'error'
      ? detail.attempt + '/' + detail.max_attempts + ' failed'
      : detail.step_status === 'complete' ? '—' : detail.attempt + '/' + detail.max_attempts;
    const badge = domainStatusBadge(detail.step_status);

    html += `<tr>
      <td style="${tdStyle}color:var(--text-primary);">${esc(domain)}</td>
      <td style="${tdStyle}">${badge}</td>
      <td style="${tdStyle}color:#f8a0a0;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;">${esc(errorText)}</td>
      <td style="${tdStyle}color:var(--text-muted);">${esc(attemptText)}</td>
    </tr>`;
  }

  html += '</tbody></table></div>';
  return html;
}

function domainStatusBadge(stepStatus) {
  if (stepStatus === 'complete') return '<span style="color:var(--accent);font-weight:500;">Complete</span>';
  if (stepStatus === 'error') return '<span style="color:var(--red);font-weight:500;">Error</span>';
  if (stepStatus === 'pending') return '<span style="color:var(--text-muted);">Pending</span>';
  return '<span style="color:var(--purple);font-weight:500;">Running</span>';
}

// ── Pipeline error actions ──

function renderPipelineErrorActions(p, dd) {
  let failedCount = 0;
  for (const dk in dd) { if (dd[dk].step_status === 'error') failedCount++; }

  const wrap = document.createElement('div');
  wrap.style.cssText = 'margin-top:12px;display:flex;gap:12px;align-items:center;';

  const retryBtn = document.createElement('button');
  retryBtn.style.cssText = 'background:var(--accent);color:var(--bg-root);border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;';
  retryBtn.textContent = 'Retry Failed (' + failedCount + ')';
  retryBtn.addEventListener('click', () => retryLegacyPipeline(p.id));
  wrap.appendChild(retryBtn);

  const skipBtn = document.createElement('button');
  skipBtn.style.cssText = 'background:none;color:var(--red);border:1px solid #5c1a1a;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;';
  skipBtn.textContent = 'Skip Step';
  skipBtn.addEventListener('click', () => skipLegacyStep(p.id, p.current_step));
  wrap.appendChild(skipBtn);

  return wrap;
}

// ── Pending removals ──

function renderPendingRemovals(p) {
  const pendingRemovals = p.pending_removals;
  const wrap = document.createElement('div');
  wrap.style.cssText = 'background:var(--red-bg);border:1px solid #3d1519;border-radius:8px;padding:12px;margin-top:12px;';

  let html = '<div style="color:var(--red);font-weight:600;margin-bottom:8px;">Inboxes need removal from campaigns</div>';

  for (const email in pendingRemovals) {
    const camps = pendingRemovals[email];
    html += `<div style="margin-bottom:8px;">`;
    html += `<div style="font-size:13px;color:#f8a0a0;">${esc(email)} is in ${camps.length} campaign(s):</div>`;
    camps.forEach(c => {
      html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 4px 16px;font-size:12px;">`;
      html += `<span style="color:var(--text-muted);">${esc(c.campaign_name)}</span>`;
      html += `<button class="remove-camp-btn" data-email="${esc(email)}" data-campaign-id="${c.campaign_id}" style="background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:11px;">Remove</button></div>`;
    });
    html += `<button class="remove-all-btn" data-email="${esc(email)}" style="background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:4px;">Remove from all campaigns</button>`;
    html += '</div>';
  }

  wrap.innerHTML = html;

  // Bind individual remove buttons
  wrap.querySelectorAll('.remove-camp-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const email = btn.dataset.email;
      const campaignId = parseInt(btn.dataset.campaignId, 10);
      removeFromCampaign(email, campaignId);
    });
  });

  // Bind remove-all buttons
  wrap.querySelectorAll('.remove-all-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      removeFromAllCampaigns(btn.dataset.email);
    });
  });

  return wrap;
}

// ── Campaign removal actions ──

async function removeFromCampaign(email, campaignId) {
  if (!confirm('Remove ' + email + ' from this campaign?')) return;
  try {
    await apiPost('/api/inbox/remove-from-campaign', { email, campaign_id: campaignId });
    showToast('Removed from campaign', 'success');
    await loadLegacyPipelines();
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function removeFromAllCampaigns(email) {
  if (!confirm('Remove ' + email + ' from ALL active campaigns?')) return;
  try {
    await apiPost('/api/inbox/remove-from-all-campaigns', { email });
    showToast('Removed from all campaigns', 'success');
    await loadLegacyPipelines();
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Legacy pipeline retry/skip ──

async function retryLegacyPipeline(pipelineId, domains) {
  try {
    const result = await apiPost('/api/pipeline/retry', { pipeline_id: pipelineId, domains: domains || [] });
    if (result.error) {
      showToast('Retry failed: ' + result.error, 'error');
    } else {
      showToast('Pipeline retrying...', 'info');
      await loadLegacyPipelines();
      startPipelinePolling();
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function skipLegacyStep(pipelineId, stepName) {
  const label = STEP_LABELS[stepName] || stepName;
  if (!confirm('Skip "' + label + '"? Domains that failed this step may have incomplete setup. This should only be used as a last resort.')) return;
  try {
    const result = await apiPost('/api/pipeline/skip-step', { pipeline_id: pipelineId });
    if (result.error) {
      showToast('Skip failed: ' + result.error, 'error');
    } else {
      showToast('Skipped ' + label + '. Moving to: ' + (STEP_LABELS[result.next_step] || result.next_step), 'success');
      await loadLegacyPipelines();
      startPipelinePolling();
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Assign to Client modal (SSE) ──

async function openAssignModal(pipelineId, groupName) {
  let clients = [];
  try {
    const data = await apiGet('/api/clients/list');
    clients = data.clients || [];
  } catch (e) { /* proceed with empty list */ }

  const content = document.createElement('div');

  // Form phase
  const form = document.createElement('div');
  form.id = 'assign-form';

  form.innerHTML = `
    <p style="font-size:13px;color:var(--text-muted);margin-bottom:16px;">Reassigning: <strong>${esc(groupName)}</strong></p>
    <label style="display:block;font-size:13px;margin-bottom:4px;">Client</label>
    <select id="ac-client-select" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);margin-bottom:12px;">
      <option value="">Select a client...</option>
      ${clients.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
      <option value="__new__">+ Add New Client</option>
    </select>
    <div id="ac-new-client-row" style="display:none;margin-bottom:12px;">
      <label style="display:block;font-size:13px;margin-bottom:4px;">New Client Name</label>
      <input type="text" id="ac-new-client-name" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);" placeholder="Client Name">
    </div>
    <label style="display:block;font-size:13px;margin-bottom:4px;">Forwarding Domain</label>
    <input type="text" id="ac-forwarding" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);margin-bottom:16px;" placeholder="e.g. clientwebsite.com">
    <button id="ac-assign-btn" disabled style="background:var(--accent);color:var(--bg-root);border:none;padding:10px 24px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;opacity:0.5;width:100%;">Assign</button>
  `;

  content.appendChild(form);

  // Progress phase (hidden initially)
  const progressWrap = document.createElement('div');
  progressWrap.id = 'assign-progress';
  progressWrap.style.display = 'none';
  content.appendChild(progressWrap);

  const panel = openModal({ title: 'Assign to Client', content });

  // Wire up form interactions
  const selectEl = panel.querySelector('#ac-client-select');
  const newRow = panel.querySelector('#ac-new-client-row');
  const newNameEl = panel.querySelector('#ac-new-client-name');
  const fwdEl = panel.querySelector('#ac-forwarding');
  const assignBtn = panel.querySelector('#ac-assign-btn');

  function checkReady() {
    const selectVal = selectEl.value;
    const newName = newNameEl.value.trim();
    const fwd = fwdEl.value.trim();
    const hasClient = selectVal === '__new__' ? newName.length > 0 : selectVal.length > 0;
    const ready = hasClient && fwd.length > 0;
    assignBtn.disabled = !ready;
    assignBtn.style.opacity = ready ? '1' : '0.5';
  }

  selectEl.addEventListener('change', () => {
    newRow.style.display = selectEl.value === '__new__' ? 'block' : 'none';
    if (selectEl.value !== '__new__') newNameEl.value = '';
    checkReady();
  });
  newNameEl.addEventListener('input', checkReady);
  fwdEl.addEventListener('input', checkReady);

  assignBtn.addEventListener('click', () => {
    const selectVal = selectEl.value;
    const isNew = selectVal === '__new__';
    const clientName = isNew ? newNameEl.value.trim() : selectVal;
    const fwd = fwdEl.value.trim();

    assignmentInProgress = true;
    form.style.display = 'none';

    const stepNames = ASSIGN_STEPS.map(s => s.label);
    const progress = sseProgress({ steps: stepNames, title: 'Assigning to ' + clientName });
    progressWrap.innerHTML = '';
    progressWrap.appendChild(progress.element);
    progressWrap.style.display = 'block';

    const resultEl = document.createElement('div');
    resultEl.style.cssText = 'margin-top:16px;';
    progressWrap.appendChild(resultEl);

    const sse = connectSSE(
      '/api/pipeline/assign-client',
      { pipeline_id: pipelineId, client_name: clientName, forwarding_domain: fwd, is_new_client: isNew },
      (event) => {
        progress.update(event);

        if (event.status === 'complete') {
          assignmentInProgress = false;
          resultEl.innerHTML = '';
          const successMsg = document.createElement('div');
          successMsg.style.cssText = 'color:var(--accent);font-weight:600;margin-bottom:8px;';
          successMsg.textContent = 'Assignment complete!';
          resultEl.appendChild(successMsg);

          const doneBtn = document.createElement('button');
          doneBtn.style.cssText = 'background:var(--accent);color:var(--bg-root);border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;';
          doneBtn.textContent = 'Done';
          doneBtn.addEventListener('click', () => {
            closeModal();
            loadAll();
          });
          resultEl.appendChild(doneBtn);
        }
      },
      (err) => {
        assignmentInProgress = false;
        resultEl.innerHTML = '';
        const errMsg = document.createElement('div');
        errMsg.style.cssText = 'color:var(--red);font-size:13px;margin-bottom:8px;';
        errMsg.textContent = 'Connection error: ' + (err?.message || 'Unknown');
        resultEl.appendChild(errMsg);

        const closeBtn = document.createElement('button');
        closeBtn.style.cssText = 'background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:8px 18px;border-radius:6px;cursor:pointer;';
        closeBtn.textContent = 'Close';
        closeBtn.addEventListener('click', closeModal);
        resultEl.appendChild(closeBtn);
      }
    );
  });
}

// ── New Pipeline modal ──

async function openNewPipelineModal() {
  let selectedType = 'generic';
  let suggestedName = 'Generic A';

  try {
    const resp = await apiGet('/api/next-generic-name');
    suggestedName = resp.name || 'Generic A';
  } catch (e) { /* use default */ }

  const content = document.createElement('div');
  content.innerHTML = `
    <label style="display:block;font-size:13px;font-weight:500;margin-bottom:6px;">Type</label>
    <div class="type-pills" style="display:flex;gap:8px;margin-bottom:16px;">
      <span class="type-pill active" data-type="generic" style="padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid var(--accent);background:var(--accent-bg);color:var(--accent);">Generic Group</span>
      <span class="type-pill" data-type="client" style="padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid var(--border);background:transparent;color:var(--text-muted);">Client</span>
      <span class="type-pill" data-type="acquisition" style="padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;border:1px solid var(--border);background:transparent;color:var(--text-muted);">Acquisition</span>
    </div>
    <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px;">Name</label>
    <input type="text" id="sp-name" value="${esc(suggestedName)}" placeholder="Generic A" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);margin-bottom:12px;">
    <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px;">Domains (one per line)</label>
    <textarea id="sp-domains" placeholder="domain1.info&#10;domain2.info&#10;domain3.info" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);min-height:100px;font-family:var(--font-mono);font-size:12px;margin-bottom:12px;resize:vertical;"></textarea>
    <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px;">Sender</label>
    <select id="sp-sender" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-primary);margin-bottom:16px;">
      <option value="sean_reynolds">Sean Reynolds</option>
      <option value="aidan_hutchinson">Aidan Hutchinson</option>
      <option value="lars_matthys">Lars Matthys</option>
    </select>
    <button id="sp-start-btn" style="background:var(--accent);color:var(--bg-root);border:none;padding:10px 24px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;width:100%;">Start Pipeline</button>
  `;

  const panel = openModal({ title: 'New Infrastructure Pipeline', content });

  const nameEl = panel.querySelector('#sp-name');
  const domainsEl = panel.querySelector('#sp-domains');
  const senderEl = panel.querySelector('#sp-sender');
  const startBtn = panel.querySelector('#sp-start-btn');
  const pills = panel.querySelectorAll('.type-pill');

  // Type pill selection
  pills.forEach(pill => {
    pill.addEventListener('click', async () => {
      pills.forEach(p => {
        p.classList.remove('active');
        p.style.border = '1px solid var(--border)';
        p.style.background = 'transparent';
        p.style.color = 'var(--text-muted)';
      });
      pill.classList.add('active');
      pill.style.border = '1px solid var(--accent)';
      pill.style.background = 'var(--accent-bg)';
      pill.style.color = 'var(--accent)';

      selectedType = pill.dataset.type;

      if (selectedType === 'generic') {
        try {
          const resp = await apiGet('/api/next-generic-name');
          nameEl.value = resp.name || '';
        } catch (e) { /* leave as-is */ }
        nameEl.placeholder = 'Generic A';
      } else {
        nameEl.value = '';
        nameEl.placeholder = selectedType === 'client' ? 'Client Name' : 'Group Name';
      }

      senderEl.value = selectedType === 'acquisition' ? 'aidan_hutchinson' : 'sean_reynolds';
    });
  });

  // Start button
  startBtn.addEventListener('click', async () => {
    const name = nameEl.value.trim();
    const domains = domainsEl.value.trim();
    const sender = senderEl.value;

    if (!name || !domains) {
      showToast('Name and domains are required', 'error');
      return;
    }

    startBtn.disabled = true;
    startBtn.textContent = 'Starting...';

    try {
      const data = await apiPost('/api/setup-pipeline/create', {
        type: selectedType,
        name,
        domains,
        sender,
      });

      if (data.error) {
        showToast('Error: ' + data.error, 'error');
        startBtn.disabled = false;
        startBtn.textContent = 'Start Pipeline';
        return;
      }

      showToast('Pipeline started!', 'success');
      closeModal();
      await loadSetupPipelines();
    } catch (e) {
      showToast('Failed: ' + e.message, 'error');
      startBtn.disabled = false;
      startBtn.textContent = 'Start Pipeline';
    }
  });
}

// ── Utilities ──

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
