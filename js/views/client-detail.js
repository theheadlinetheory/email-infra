/**
 * Client detail view — accounts table, health stats, trend chart, actions.
 * Loaded via #client/{id} route.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost, apiGet, connectSSE } from '../core/api.js';
import { openModal, closeModal } from '../components/modal.js';
import { sseProgress } from '../components/sse-progress.js';
import { showToast } from '../components/toast.js';

const DELETE_STEPS = [
  'Removing from campaigns',
  'Deleting SmartLead accounts',
  'Cancelling Zapmail domains',
  'Updating Google Sheet',
  'Deleting SmartLead client',
];

const TRANSITION_STEPS = [
  'Setting up new SmartLead client',
  'Updating SmartLead tags',
  'Verifying client assignment',
  'Updating Zapmail domain tags',
  'Setting forwarding domain',
  'Updating Google Sheet',
];

let container = null;
let unsubs = [];
let clientId = null;
let trendChart = null;
let currentDays = 30;
let accountsData = null;
let trendsData = null;
let sseHandle = null;

export function mount(el) {
  container = el;
  container.className = 'client-detail';
  clientId = location.hash.split('/')[1] || null;
  if (!clientId) {
    el.innerHTML = '<p style="color:var(--text-muted);padding:24px;">No client selected</p>';
    return;
  }

  const acctKey = `clientAccounts_${clientId}`;
  const trendKey = `clientTrends_${clientId}`;
  unsubs.push(store.subscribe(acctKey, onAccountsUpdate));
  unsubs.push(store.subscribe(trendKey, onTrendsUpdate));
  unsubs.push(store.subscribe('loading', renderIfReady));
  unsubs.push(store.subscribe('errors', renderIfReady));

  container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading client data...</div>';
  load();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  destroyChart();
  if (sseHandle) { sseHandle.close(); sseHandle = null; }
  if (container) container.className = '';
  container = null;
  clientId = null;
  accountsData = null;
  trendsData = null;
  currentDays = 30;
}

// ── Data Loading ──

async function load() {
  if (!clientId) return;
  const acctKey = `clientAccounts_${clientId}`;
  try {
    await Promise.all([
      fetchSlice(acctKey, `/api/client/${clientId}/accounts`),
      loadTrends(currentDays),
    ]);
  } catch (e) { /* errors managed by store */ }
}

async function loadTrends(days) {
  if (!clientId) return;
  currentDays = days;
  const trendKey = `clientTrends_${clientId}`;
  store.setLoading(trendKey, true);
  try {
    const result = await apiGet(`/api/client/${clientId}/trends?days=${days}`);
    store.setData(trendKey, result);
  } catch (e) {
    store.setError(trendKey, e.message);
    store.setLoading(trendKey, false);
  }
}

function onAccountsUpdate() {
  const key = `clientAccounts_${clientId}`;
  accountsData = store.get(key);
  renderIfReady();
}

function onTrendsUpdate() {
  const key = `clientTrends_${clientId}`;
  trendsData = store.get(key);
  renderIfReady();
}

// ── Rendering ──

function renderIfReady() {
  if (!container || !clientId) return;
  const acctKey = `clientAccounts_${clientId}`;
  const loading = store.get('loading');
  const errors = store.get('errors');

  const acctLoading = loading?.[acctKey];
  const acctError = errors?.[acctKey];

  if (acctLoading && !accountsData) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading client data...</div>';
    return;
  }

  if (acctError && !accountsData) {
    container.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'error-card';
    card.innerHTML = `<div class="error-msg">${esc(acctError)}</div>`;
    const retryBtn = document.createElement('button');
    retryBtn.className = 'retry-btn';
    retryBtn.textContent = 'Retry';
    retryBtn.addEventListener('click', load);
    card.appendChild(retryBtn);
    container.appendChild(card);
    return;
  }

  if (!accountsData) return;
  render();
}

function render() {
  if (!container || !accountsData) return;
  destroyChart();

  const data = accountsData;
  const trends = trendsData;
  const accounts = data.accounts || [];
  const isArchived = data.archived || false;

  container.innerHTML = '';

  // ── Header with back button + archive ──
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;';

  const leftHeader = document.createElement('div');
  leftHeader.style.cssText = 'display:flex;align-items:center;gap:14px;';

  const backBtn = document.createElement('button');
  backBtn.textContent = '← Back';
  backBtn.style.cssText = 'background:var(--bg-raised);color:var(--text-muted);border:1px solid var(--border);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;';
  backBtn.addEventListener('click', () => { location.hash = '#overview'; });

  const titleBlock = document.createElement('div');
  titleBlock.innerHTML = `<h2 style="font-size:22px;font-weight:600;letter-spacing:-0.3px;margin:0;font-family:var(--font-display);">${esc(data.client_name || 'Client')}</h2>
    <span style="font-size:13px;color:var(--text-muted);font-family:var(--font-mono);">${accounts.length} account${accounts.length !== 1 ? 's' : ''}</span>`;

  leftHeader.appendChild(backBtn);
  leftHeader.appendChild(titleBlock);

  const actionRow = document.createElement('div');
  actionRow.style.cssText = 'display:flex;gap:8px;align-items:center;';

  const genericBtn = document.createElement('button');
  genericBtn.textContent = 'Convert to Generic';
  genericBtn.style.cssText = 'background:var(--purple);color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;';
  genericBtn.addEventListener('click', () => openConvertToGenericModal(data));

  const transBtn2 = document.createElement('button');
  transBtn2.textContent = 'Transition';
  transBtn2.style.cssText = 'background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;';
  transBtn2.addEventListener('click', () => openTransitionModal(data));

  const delBtn2 = document.createElement('button');
  delBtn2.textContent = 'Delete';
  delBtn2.style.cssText = 'background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;';
  delBtn2.addEventListener('click', () => openDeleteModal(data));

  const archBtn = document.createElement('button');
  archBtn.textContent = isArchived ? 'Unarchive' : 'Archive';
  archBtn.style.cssText = `background:none;border:1px solid var(--border);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;color:${isArchived ? '#22c55e' : 'var(--text-muted)'};`;
  archBtn.addEventListener('click', () => toggleArchive(data));

  actionRow.appendChild(genericBtn);
  actionRow.appendChild(transBtn2);
  actionRow.appendChild(delBtn2);
  actionRow.appendChild(archBtn);

  header.appendChild(leftHeader);
  header.appendChild(actionRow);
  container.appendChild(header);

  // ── Infrastructure Replacement Banner ──
  if (data.flagged_domains && data.flagged_domains.length > 0) {
    const banner = document.createElement('div');
    banner.style.cssText = 'background:var(--red-bg);border:1px solid #3d1519;border-radius:8px;padding:14px 18px;margin-bottom:16px;';
    banner.innerHTML = `
      <div style="font-size:14px;color:var(--red);font-weight:600;margin-bottom:6px;">Infrastructure Replacement Needed</div>
      <div style="font-size:13px;color:#f8a0a0;">${data.flagged_inbox_count} inbox(es) across ${data.flagged_domains.length} domain(s) are unhealthy.</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">Flagged domains: ${esc(data.flagged_domains.join(', '))}</div>
      <div style="font-size:12px;color:#fbbf24;padding:6px 10px;background:rgba(251,191,36,0.1);border-radius:4px;">Replacements will go to the B group once set up. 1-for-1 replacement is disabled.</div>
    `;
    container.appendChild(banner);
  }

  // ── Trend Chart ──
  if (trends && !trends.error) {
    container.appendChild(buildChartSection(trends));
  }

  // ── Idle Inbox Alert ──
  const idleInboxes = accounts.filter(a =>
    a.warmup_status !== 'ACTIVE' && a.campaign_count === 0 && a.warmup_days !== null && a.warmup_days >= 14
  );
  if (idleInboxes.length > 0) {
    const idleAlert = document.createElement('div');
    idleAlert.style.cssText = 'background:var(--yellow-bg);border:1px solid #f59e0b33;border-radius:8px;padding:14px 18px;margin-bottom:16px;';
    idleAlert.innerHTML = `
      <div style="font-size:14px;color:var(--yellow);font-weight:600;margin-bottom:6px;">${idleInboxes.length} warmed inbox(es) not in any campaign</div>
      <div style="font-size:13px;color:#ffd9aa;">${esc(idleInboxes.map(a => a.email).join(', '))}</div>
    `;
    container.appendChild(idleAlert);
  }

  // ── Accounts Table ──
  if (accounts.length > 0) {
    container.appendChild(buildAccountsTable(accounts, data));
  }


  // ── Render Chart After DOM ──
  setTimeout(() => {
    const canvas = container?.querySelector('#trend-chart');
    if (canvas && trends && !trends.error && trends.data && trends.data.some(d => d.reply_rate !== null)) {
      renderTrendChart(canvas, trends);
    } else if (canvas) {
      renderNoChartData(canvas, 'No campaign data yet');
    }
  }, 50);
}

// ── Chart Section ──

function buildChartSection(trends) {
  const s = trends.summary || {};
  const ti = trendIndicator(s.trend);
  const section = document.createElement('div');
  section.style.cssText = 'background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px;';

  const topRow = document.createElement('div');
  topRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;';

  const titleEl = document.createElement('div');
  titleEl.style.cssText = 'font-size:14px;font-weight:600;';
  titleEl.innerHTML = 'Campaign Performance <span style="font-size:11px;color:var(--text-muted);font-weight:400;">(7-day rolling avg)</span>';

  const zoomBtns = document.createElement('div');
  zoomBtns.style.cssText = 'display:flex;gap:4px;';

  const ranges = [
    { label: '7D', days: 7 },
    { label: '14D', days: 14 },
    { label: '30D', days: 30 },
    { label: '90D', days: 90 },
    { label: 'All', days: 0 },
  ];

  for (const r of ranges) {
    const btn = document.createElement('button');
    btn.textContent = r.label;
    btn.dataset.days = r.days;
    const isActive = r.days === currentDays;
    btn.style.cssText = `background:var(--bg-surface);color:${isActive ? 'var(--accent)' : 'var(--text-muted)'};border:1px solid ${isActive ? '#4ecdc4' : 'var(--border)'};padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;`;
    btn.addEventListener('click', () => {
      loadTrends(r.days);
    });
    zoomBtns.appendChild(btn);
  }

  topRow.appendChild(titleEl);
  topRow.appendChild(zoomBtns);
  section.appendChild(topRow);

  const chartWrap = document.createElement('div');
  chartWrap.style.cssText = 'height:200px;position:relative;';
  const canvas = document.createElement('canvas');
  canvas.id = 'trend-chart';
  chartWrap.appendChild(canvas);
  section.appendChild(chartWrap);

  const summary = document.createElement('div');
  summary.id = 'trend-summary';
  summary.style.cssText = 'display:flex;gap:16px;margin-top:12px;font-size:12px;flex-wrap:wrap;';
  summary.innerHTML = buildSummaryHtml(s, ti);
  section.appendChild(summary);

  return section;
}

function buildSummaryHtml(s, ti) {
  return `
    <div><span style="color:var(--accent);">&#9679;</span> <span style="color:var(--text-muted);">Reply Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_reply_rate || 0}%</span></div>
    <div><span style="color:var(--red);">&#9679;</span> <span style="color:var(--text-muted);">Bounce Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_bounce_rate || 0}%</span></div>
    <div><span style="color:var(--text-muted);">Last 7d:</span> <span style="color:${ti.color};font-weight:600;">${s.recent_7d_rate || 0}% ${ti.icon}</span></div>
    <div><span style="color:var(--text-muted);">Prior 7d:</span> <span style="color:var(--text-muted);">${s.prior_7d_rate || 0}%</span></div>
    <div><span style="color:var(--text-muted);">Sent:</span> <span style="color:var(--text-primary);">${(s.total_sent || 0).toLocaleString()}</span></div>
    <div><span style="color:var(--text-muted);">Bounced:</span> <span style="color:var(--text-primary);">${(s.total_bounced || 0).toLocaleString()}</span></div>
    <div><span style="color:var(--text-muted);">Replies:</span> <span style="color:var(--text-primary);">${s.total_replied || 0}</span></div>
  `;
}

function trendIndicator(trend) {
  const icon = trend === 'up' ? '▲' : trend === 'down' ? '▼' : '▶';
  const color = trend === 'up' ? '#22c55e' : trend === 'down' ? '#ef4444' : '#9ca3af';
  return { icon, color };
}

function renderNoChartData(canvas, message) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#666';
  ctx.font = '14px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(message, canvas.width / 2, 100);
}

function renderTrendChart(canvas, trends) {
  if (!canvas || !window.Chart) return;
  destroyChart();

  const points = trends.data.filter(d => d.reply_rate !== null);
  if (points.length === 0) {
    renderNoChartData(canvas, 'No sending data in this period');
    return;
  }

  // Update summary
  const s = trends.summary || {};
  const ti = trendIndicator(s.trend);
  const summaryEl = container?.querySelector('#trend-summary');
  if (summaryEl) {
    summaryEl.innerHTML = buildSummaryHtml(s, ti);
  }

  const ctx = canvas.getContext('2d');
  trendChart = new window.Chart(ctx, {
    type: 'line',
    data: {
      labels: points.map(d => d.date),
      datasets: [{
        label: 'Reply Rate',
        data: points.map(d => d.reply_rate),
        borderColor: '#22c55e',
        backgroundColor: 'rgba(64, 224, 208, 0.08)',
        borderWidth: 2,
        pointRadius: 3,
        pointBackgroundColor: '#22c55e',
        pointHoverRadius: 6,
        fill: true,
        tension: 0.3,
      }, {
        label: 'Bounce Rate',
        data: points.map(d => d.bounce_rate),
        borderColor: '#ef4444',
        backgroundColor: 'rgba(239, 68, 68, 0.05)',
        borderWidth: 2,
        pointRadius: 2,
        pointBackgroundColor: '#ef4444',
        pointHoverRadius: 5,
        fill: true,
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: { color: '#9ca3af', boxWidth: 12, padding: 12, font: { size: 11 } },
        },
        tooltip: {
          backgroundColor: '#16213e',
          borderColor: '#e5e7eb',
          borderWidth: 1,
          titleColor: '#eee',
          bodyColor: '#eee',
          callbacks: {
            label(context) {
              const pt = points[context.dataIndex];
              if (context.datasetIndex === 0) {
                return `Reply Rate: ${pt.reply_rate}% (${pt.replied}/${pt.sent.toLocaleString()} sent)`;
              }
              return `Bounce Rate: ${pt.bounce_rate}% (${pt.bounced}/${pt.sent.toLocaleString()} sent)`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#666', font: { size: 10 }, maxTicksLimit: 12 },
          grid: { color: '#1a2744' },
        },
        y: {
          beginAtZero: true,
          ticks: { color: '#666', font: { size: 10 }, callback: v => v + '%' },
          grid: { color: '#1a2744' },
        },
      },
    },
  });
}

function destroyChart() {
  if (trendChart) { trendChart.destroy(); trendChart = null; }
}

// ── Accounts Table ──

function buildAccountsTable(accounts, data) {
  const wrapper = document.createElement('div');

  const title = document.createElement('h3');
  title.style.cssText = 'font-size:15px;font-weight:600;margin:20px 0 12px;color:var(--text-primary);font-family:var(--font-display);';
  title.textContent = `Email Accounts (${accounts.length})`;
  wrapper.appendChild(title);

  const scroll = document.createElement('div');
  scroll.style.cssText = 'overflow-x:auto;';

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Email</th><th>Health</th><th>Warmup</th><th>Rep</th><th>Bounce</th><th>Reply</th><th>Sent</th><th>Campaigns</th><th>SMTP</th></tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const a of accounts) {
    tbody.appendChild(buildAccountRow(a));
  }
  table.appendChild(tbody);
  scroll.appendChild(table);
  wrapper.appendChild(scroll);
  return wrapper;
}

function buildAccountRow(a) {
  const tr = document.createElement('tr');

  // Row background for flagged domains
  if (a.domain_flagged) {
    tr.style.background = '#2a1a1a';
  }

  // Email
  const tdEmail = document.createElement('td');
  tdEmail.textContent = a.email;
  tr.appendChild(tdEmail);

  // Health score + flags
  const tdHealth = document.createElement('td');
  const score = a.health_score;
  const healthColor = score >= 85 ? '#22c55e' : score >= 60 ? '#f59e0b' : '#ef4444';
  const healthBg = score >= 85 ? '#f0fdf4' : score >= 60 ? '#fffbeb' : '#fef2f2';

  let healthHtml = `<span style="background:${healthBg};color:${healthColor};padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;">${score}</span>`;

  const flagMap = { bounce: 'B', reply: 'R', reputation: 'REP', placement: 'P', smtp: 'SMTP', blocked: 'BLK', warmup_off: 'WU' };
  const flags = a.health_flags || [];
  for (const f of flags) {
    healthHtml += `<span style="background:var(--red-bg);color:var(--red);padding:1px 4px;border-radius:3px;font-size:10px;margin-left:2px;" title="${esc(f)}">${flagMap[f] || esc(f)}</span>`;
  }
  tdHealth.innerHTML = healthHtml;
  tr.appendChild(tdHealth);

  // Warmup status + progress bar
  const tdWarmup = document.createElement('td');
  const statusColor = a.warmup_status === 'ACTIVE' ? '#22c55e' : (a.blocked_reason ? '#ef4444' : '#f59e0b');
  let warmupHtml = `<span style="color:${statusColor}">${esc(a.warmup_status)}</span>`;

  if (a.warmup_status === 'ACTIVE' && a.warmup_days !== null && a.warmup_days < 14) {
    const pct = Math.min(100, Math.round(a.warmup_days / 14 * 100));
    warmupHtml += `<div style="margin-top:4px;background:var(--bg-input);border-radius:3px;height:4px;width:80px;"><div style="background:var(--purple);height:100%;width:${pct}%;border-radius:3px;"></div></div>`;
    warmupHtml += `<div style="font-size:10px;color:var(--text-muted);margin-top:2px;">${a.warmup_days}d / 14d</div>`;
  }
  if (a.blocked_reason) {
    let reason = a.blocked_reason;
    if (reason.includes('<') && reason.length > 120) {
      const titleMatch = reason.match(/<title[^>]*>([^<]+)<\/title>/i);
      reason = titleMatch ? titleMatch[1].trim() : 'DNS/connection error';
    }
    if (reason.length > 80) reason = reason.slice(0, 77) + '...';
    warmupHtml += `<br><small style="color:var(--red)">${esc(reason)}</small>`;
  }
  tdWarmup.innerHTML = warmupHtml;
  tr.appendChild(tdWarmup);

  // Reputation
  const tdRep = document.createElement('td');
  tdRep.textContent = a.warmup_reputation ?? '';
  tr.appendChild(tdRep);

  // Bounce rate
  const tdBounce = document.createElement('td');
  const br = a.bounce_rate !== null ? parseFloat(a.bounce_rate) : null;
  const brColor = br !== null ? (br > 3 ? '#ef4444' : br > 1 ? '#f59e0b' : '#22c55e') : '#9ca3af';
  tdBounce.style.color = brColor;
  tdBounce.textContent = br !== null ? br.toFixed(1) + '%' : '—';
  tr.appendChild(tdBounce);

  // Reply rate
  const tdReply = document.createElement('td');
  const rr = a.reply_rate !== null ? parseFloat(a.reply_rate) : null;
  const rrColor = rr !== null ? (rr > 5 ? '#22c55e' : rr > 2 ? '#f59e0b' : '#ef4444') : '#9ca3af';
  tdReply.style.color = rrColor;
  tdReply.textContent = rr !== null ? rr.toFixed(1) + '%' : '—';
  tr.appendChild(tdReply);

  // Sent
  const tdSent = document.createElement('td');
  tdSent.textContent = a.health_sent || a.warmup_sent || '';
  tr.appendChild(tdSent);

  // Campaigns
  const tdCamp = document.createElement('td');
  tdCamp.textContent = a.campaign_count ?? '';
  tr.appendChild(tdCamp);

  // SMTP
  const tdSmtp = document.createElement('td');
  tdSmtp.style.color = a.smtp_ok ? '#22c55e' : '#ef4444';
  tdSmtp.textContent = a.smtp_ok ? 'OK' : 'FAIL';
  tr.appendChild(tdSmtp);

  return tr;
}

// ── Archive Action ──

async function toggleArchive(data) {
  const isArchived = data.archived || false;
  const action = isArchived ? 'unarchive' : 'archive';
  if (!confirm(`${action.charAt(0).toUpperCase() + action.slice(1)} "${data.client_name}"?`)) return;
  try {
    const result = await apiPost('/api/client/archive', {
      client_name: data.client_name,
      archived: !isArchived,
    });
    if (result.ok) {
      showToast(`Client ${action}d`, 'success');
      location.hash = '#overview';
    }
  } catch (e) {
    showToast(`Archive error: ${e.message}`, 'error');
  }
}

// ── Convert to Generic Modal ──

async function openConvertToGenericModal(data) {
  const content = document.createElement('div');
  content.innerHTML = `<div style="text-align:center;padding:12px;"><span class="spinner"></span> Fetching next generic name...</div>`;
  openModal({ title: 'Convert to Generic Group', content });

  let genericName;
  try {
    const resp = await apiGet('/api/next-generic-name');
    genericName = resp.name || resp.generic_name;
  } catch (e) {
    content.innerHTML = `<p style="color:var(--red);">Failed to get generic name: ${esc(e.message)}</p>`;
    return;
  }

  content.innerHTML = '';

  const info = document.createElement('div');
  info.style.cssText = 'margin-bottom:16px;';
  info.innerHTML = `
    <p style="font-size:14px;color:var(--text-secondary);margin-bottom:12px;">
      This will convert <strong>${esc(data.client_name)}</strong>'s ${(data.accounts || []).length} accounts into a generic group for reuse with future clients.
    </p>
    <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:12px;">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px;">New group name</div>
      <div style="font-size:16px;font-weight:600;color:var(--purple);">${esc(genericName)}</div>
    </div>
    <p style="font-size:13px;color:var(--text-muted);">Accounts will be removed from campaigns, retagged, and made available for assignment.</p>
  `;
  content.appendChild(info);

  const fwdDiv = document.createElement('div');
  fwdDiv.style.cssText = 'margin-bottom:16px;';
  fwdDiv.innerHTML = `<label style="display:block;font-size:13px;color:var(--text-muted);margin-bottom:4px;">Forwarding Domain (for the generic group)</label>`;
  const fwdInput = document.createElement('input');
  fwdInput.type = 'text';
  fwdInput.placeholder = 'e.g. theheadlinetheory.com';
  fwdInput.value = 'theheadlinetheory.com';
  fwdInput.style.cssText = 'width:100%;padding:9px 12px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius);font-size:14px;';
  fwdDiv.appendChild(fwdInput);
  content.appendChild(fwdDiv);

  const convertBtn = document.createElement('button');
  convertBtn.textContent = 'Convert to Generic';
  convertBtn.style.cssText = 'width:100%;padding:10px;border:none;border-radius:6px;background:var(--purple);color:#fff;font-weight:600;cursor:pointer;font-size:14px;';
  convertBtn.addEventListener('click', () => {
    convertBtn.disabled = true;
    convertBtn.textContent = 'Converting...';
    info.style.display = 'none';
    fwdDiv.style.display = 'none';

    const progressWrap = document.createElement('div');
    const progress = sseProgress({ steps: TRANSITION_STEPS, title: '' });
    progressWrap.appendChild(progress.element);
    content.appendChild(progressWrap);

    sseHandle = connectSSE(
      '/api/client/transition',
      {
        client_id: data.client_id,
        client_name: data.client_name,
        new_client_name: genericName,
        forwarding_domain: fwdInput.value.trim() || 'theheadlinetheory.com',
        is_new_client: true,
      },
      (event) => {
        if (event.status === 'complete') {
          progress.update({ step: TRANSITION_STEPS.length, total: TRANSITION_STEPS.length, status: 'done', message: 'Complete' });
          showDoneButton(`Converted to ${genericName}!`, () => {
            closeModal();
            location.hash = '#overview';
          });
          return;
        }
        progress.update({
          step: event.step,
          total: event.total || TRANSITION_STEPS.length,
          status: event.status,
          message: event.message,
        });
        if (event.status === 'error') {
          showDoneButton(event.message, closeModal, true);
        }
      },
      (err) => {
        showToast(`Convert error: ${err.message}`, 'error');
      },
    );
  });
  content.appendChild(convertBtn);
}

// ── Delete Modal ──

function openDeleteModal(data) {
  const content = document.createElement('div');

  // Step 1: confirmation warning
  const step1 = document.createElement('div');
  step1.id = 'del-step1';
  step1.innerHTML = `
    <p style="font-size:14px;color:var(--text-secondary);margin-bottom:16px;">
      This will permanently delete <strong style="color:var(--red);">${esc(data.client_name)}</strong>'s entire infrastructure:
      all SmartLead accounts, Zapmail domains, campaign associations, and Sheet records.
    </p>
    <p style="font-size:13px;color:var(--text-muted);margin-bottom:16px;">This action cannot be undone.</p>
  `;

  const nextBtn = document.createElement('button');
  nextBtn.className = 'btn btn-primary';
  nextBtn.style.cssText = 'background:var(--red);';
  nextBtn.textContent = 'I understand, continue';
  nextBtn.addEventListener('click', () => {
    step1.style.display = 'none';
    step2.style.display = 'block';
    confirmInput.focus();
  });
  step1.appendChild(nextBtn);

  // Step 2: type name to confirm
  const step2 = document.createElement('div');
  step2.id = 'del-step2';
  step2.style.display = 'none';

  const confirmLabel = document.createElement('label');
  confirmLabel.innerHTML = `Type <strong>${esc(data.client_name)}</strong> to confirm:`;
  confirmLabel.style.cssText = 'display:block;font-size:13px;color:var(--text-muted);margin-bottom:8px;';

  const confirmInput = document.createElement('input');
  confirmInput.type = 'text';
  confirmInput.placeholder = data.client_name;
  confirmInput.style.cssText = 'width:100%;padding:9px 12px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius);font-size:14px;margin-bottom:12px;';

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'btn btn-primary';
  deleteBtn.style.cssText = 'background:var(--red);opacity:0.5;';
  deleteBtn.textContent = 'Delete All Infrastructure';
  deleteBtn.disabled = true;

  confirmInput.addEventListener('input', () => {
    const ready = confirmInput.value.trim().toLowerCase() === data.client_name.trim().toLowerCase();
    deleteBtn.disabled = !ready;
    deleteBtn.style.opacity = ready ? '1' : '0.5';
  });

  deleteBtn.addEventListener('click', () => {
    step2.style.display = 'none';
    progressWrap.style.display = 'block';
    startDelete(data, progress);
  });

  step2.appendChild(confirmLabel);
  step2.appendChild(confirmInput);
  step2.appendChild(deleteBtn);

  // Step 3: SSE progress
  const progressWrap = document.createElement('div');
  progressWrap.style.display = 'none';

  const progressTitle = document.createElement('div');
  progressTitle.style.cssText = 'font-size:13px;color:var(--text-muted);margin-bottom:12px;';
  progressTitle.innerHTML = `Deleting infrastructure for <strong>${esc(data.client_name)}</strong>...`;
  progressWrap.appendChild(progressTitle);

  const progress = sseProgress({ steps: DELETE_STEPS, title: '' });
  progressWrap.appendChild(progress.element);

  const resultEl = document.createElement('div');
  resultEl.style.display = 'none';
  progressWrap.appendChild(resultEl);

  content.appendChild(step1);
  content.appendChild(step2);
  content.appendChild(progressWrap);

  openModal({ title: 'Delete Infrastructure', content });
}

function startDelete(data, progress) {
  sseHandle = connectSSE(
    '/api/client/delete-infra',
    { client_id: data.client_id, client_name: data.client_name },
    (event) => {
      if (event.status === 'complete') {
        progress.update({ step: DELETE_STEPS.length, total: DELETE_STEPS.length, status: 'done', message: 'Complete' });
        showDoneButton('Infrastructure deleted successfully.', () => {
          closeModal();
          location.hash = '#overview';
        });
        return;
      }
      progress.update({
        step: event.step,
        total: event.total || DELETE_STEPS.length,
        status: event.status,
        message: event.message,
      });
      if (event.status === 'error') {
        showDoneButton(event.message, closeModal, true);
      }
    },
    (err) => {
      showToast(`Delete error: ${err.message}`, 'error');
    },
  );
}

// ── Transition Modal ──

function openTransitionModal(data) {
  const content = document.createElement('div');

  // Form
  const form = document.createElement('div');
  form.id = 'transition-form';

  const sourceLabel = document.createElement('div');
  sourceLabel.style.cssText = 'font-size:13px;color:var(--text-muted);margin-bottom:16px;';
  sourceLabel.innerHTML = `Transitioning from: <strong>${esc(data.client_name)}</strong>`;
  form.appendChild(sourceLabel);

  // Client select
  const clientLabel = document.createElement('label');
  clientLabel.textContent = 'Target Client';
  form.appendChild(clientLabel);

  const clientSelect = document.createElement('select');
  clientSelect.innerHTML = '<option value="">Loading...</option>';
  form.appendChild(clientSelect);

  // New client name (hidden by default)
  const newClientRow = document.createElement('div');
  newClientRow.style.cssText = 'display:none;margin-top:10px;';
  const newClientLabel = document.createElement('label');
  newClientLabel.textContent = 'New Client Name';
  const newClientInput = document.createElement('input');
  newClientInput.type = 'text';
  newClientInput.placeholder = 'Enter new client name';
  newClientRow.appendChild(newClientLabel);
  newClientRow.appendChild(newClientInput);
  form.appendChild(newClientRow);

  // Forwarding domain
  const fwdDiv = document.createElement('div');
  fwdDiv.style.cssText = 'margin-top:10px;';
  const fwdLabel = document.createElement('label');
  fwdLabel.textContent = 'Forwarding Domain';
  const fwdInput = document.createElement('input');
  fwdInput.type = 'text';
  fwdInput.placeholder = 'e.g. clientwebsite.com';
  fwdDiv.appendChild(fwdLabel);
  fwdDiv.appendChild(fwdInput);
  form.appendChild(fwdDiv);

  // Start button
  const btnRow = document.createElement('div');
  btnRow.className = 'btn-row';
  btnRow.style.cssText = 'margin-top:16px;';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-cancel';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', closeModal);

  const startBtn = document.createElement('button');
  startBtn.className = 'btn btn-primary';
  startBtn.textContent = 'Start Transition';
  startBtn.disabled = true;
  startBtn.style.opacity = '0.5';

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(startBtn);
  form.appendChild(btnRow);

  // Populate client dropdown
  loadClientDropdown(clientSelect, data.client_name);

  clientSelect.addEventListener('change', () => {
    const val = clientSelect.value;
    newClientRow.style.display = val === '__new__' ? 'block' : 'none';
    if (val !== '__new__') newClientInput.value = '';
    checkTransitionReady();
  });
  newClientInput.addEventListener('input', checkTransitionReady);
  fwdInput.addEventListener('input', checkTransitionReady);

  function checkTransitionReady() {
    const selectVal = clientSelect.value;
    const newName = newClientInput.value.trim();
    const fwd = fwdInput.value.trim();
    const hasClient = selectVal === '__new__' ? newName.length > 0 : selectVal.length > 0;
    const ready = hasClient && fwd.length > 0;
    startBtn.disabled = !ready;
    startBtn.style.opacity = ready ? '1' : '0.5';
  }

  // Progress
  const progressWrap = document.createElement('div');
  progressWrap.style.display = 'none';

  const progressTitle = document.createElement('div');
  progressTitle.id = 'tr-progress-label';
  progressTitle.style.cssText = 'font-size:13px;color:var(--text-muted);margin-bottom:12px;';
  progressWrap.appendChild(progressTitle);

  const progress = sseProgress({ steps: TRANSITION_STEPS, title: '' });
  progressWrap.appendChild(progress.element);

  startBtn.addEventListener('click', () => {
    const selectVal = clientSelect.value;
    const isNew = selectVal === '__new__';
    const newClientName = isNew ? newClientInput.value.trim() : selectVal;
    const fwd = fwdInput.value.trim();

    form.style.display = 'none';
    progressWrap.style.display = 'block';
    progressTitle.innerHTML = `Transitioning to <strong>${esc(newClientName)}</strong>...`;

    startTransition(data, newClientName, fwd, isNew, progress);
  });

  content.appendChild(form);
  content.appendChild(progressWrap);

  openModal({ title: 'Transition Infrastructure', content });
}

async function loadClientDropdown(select, excludeClient) {
  try {
    const resp = await apiGet('/api/clients/list');
    let clients = resp.clients || [];
    if (excludeClient) {
      clients = clients.filter(c => c !== excludeClient);
    }
    let opts = '<option value="">Select a client...</option>';
    for (const c of clients) {
      opts += `<option value="${esc(c)}">${esc(c)}</option>`;
    }
    opts += '<option value="__new__">+ Add New Client</option>';
    select.innerHTML = opts;
  } catch (e) {
    select.innerHTML = '<option value="">Error loading clients</option>';
  }
}

function startTransition(data, newClientName, fwd, isNew, progress) {
  sseHandle = connectSSE(
    '/api/client/transition',
    {
      client_id: data.client_id,
      client_name: data.client_name,
      new_client_name: newClientName,
      forwarding_domain: fwd,
      is_new_client: isNew,
    },
    (event) => {
      if (event.status === 'complete') {
        progress.update({ step: TRANSITION_STEPS.length, total: TRANSITION_STEPS.length, status: 'done', message: 'Complete' });
        showDoneButton('Transition complete!', () => {
          closeModal();
          location.hash = '#overview';
        });
        return;
      }
      progress.update({
        step: event.step,
        total: event.total || TRANSITION_STEPS.length,
        status: event.status,
        message: event.message,
      });
      if (event.status === 'error') {
        showDoneButton(event.message, closeModal, true);
      }
    },
    (err) => {
      showToast(`Transition error: ${err.message}`, 'error');
    },
  );
}

// ── Shared Helpers ──

function showDoneButton(message, onDone, isError = false) {
  const modal = document.querySelector('.modal-panel');
  if (!modal) return;

  const result = document.createElement('div');
  result.style.cssText = 'margin-top:16px;';

  const msg = document.createElement('div');
  msg.style.cssText = `color:${isError ? 'var(--red)' : 'var(--accent)'};font-weight:600;margin-bottom:8px;font-size:13px;`;
  msg.textContent = message;
  result.appendChild(msg);

  const doneBtn = document.createElement('button');
  doneBtn.style.cssText = `background:${isError ? 'var(--bg-raised)' : 'var(--accent)'};color:${isError ? 'var(--text-primary)' : 'var(--bg-root)'};border:${isError ? '1px solid var(--border)' : 'none'};padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;`;
  doneBtn.textContent = 'Done';
  doneBtn.addEventListener('click', onDone);
  result.appendChild(doneBtn);

  modal.appendChild(result);
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
