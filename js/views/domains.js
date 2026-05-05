/**
 * Domains view — registrar domains, expiry tracking, auto-renew management.
 * Per-registrar tables with toggle buttons and bulk disable.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost } from '../core/api.js';
import { showToast } from '../components/toast.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('domains', render));
  unsubs.push(store.subscribe('loading', render));
  unsubs.push(store.subscribe('errors', render));
  load();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  container = null;
}

async function load() {
  try {
    await fetchSlice('domains', '/api/domains');
  } catch (e) { /* handled by store */ }
}

function render() {
  if (!container) return;
  const data = store.get('domains');
  const loading = store.get('loading')?.domains;
  const error = store.get('errors')?.domains;

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading domain registrar data...</div>';
    return;
  }
  if (error && !data) {
    container.innerHTML = `<div class="error-card"><div class="error-msg">${esc(error)}</div><button class="retry-btn">Retry</button></div>`;
    container.querySelector('.retry-btn')?.addEventListener('click', load);
    return;
  }
  if (!data) return;

  let html = '';

  // --- Summary Row ---
  html += '<div class="summary-row">';
  html += statCardHtml(data.total_domains || 0, 'Total Domains', 'good');
  html += statCardHtml(data.expiring_soon || 0, 'Expiring in 14 days', (data.expiring_soon || 0) > 0 ? 'alert' : 'good');
  html += statCardHtml(data.no_auto_renew_30d || 0, 'No Auto-Renew (30d)', (data.no_auto_renew_30d || 0) > 0 ? 'warn' : 'good');
  html += '</div>';

  // --- Alert Banner ---
  const alerts = data.alerts || [];
  if (alerts.length > 0) {
    html += '<div class="alert-banner"><h3>Domain Expiry Alerts</h3>';
    alerts.forEach(a => {
      html += `<div class="alert-item">${esc(a.domain)} (${esc(a.registrar)}) — expires ${esc(a.expires)}, ${esc(String(a.days_until_expiry))} days left, auto-renew OFF</div>`;
    });
    html += '</div>';
  }

  // --- Bulk Disable Button ---
  const byRegistrar = data.by_registrar || {};
  let allAutoRenewOn = [];
  for (const domains of Object.values(byRegistrar)) {
    domains.forEach(dm => {
      if (dm.auto_renew) allAutoRenewOn.push({ domain: dm.domain, registrar: dm.registrar });
    });
  }

  if (allAutoRenewOn.length > 0) {
    html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;padding:12px 16px;background:var(--yellow-bg);border:1px solid var(--yellow);border-radius:var(--radius);font-size:13px;">
      <span style="color:var(--yellow);font-weight:600;">${allAutoRenewOn.length} domains have auto-renew ON</span>
      <button id="bulk-disable-btn" style="background:var(--red);color:#fff;border:none;padding:6px 16px;border-radius:var(--radius);cursor:pointer;font-weight:600;font-size:12px;">Disable All Auto-Renew</button>
    </div>`;
  }

  // --- Domain Tables by Registrar ---
  for (const [registrar, domains] of Object.entries(byRegistrar)) {
    html += `<h2 class="section-title">${esc(registrar)} (${domains.length} domains)</h2>`;
    html += '<table class="zm-domain-table"><thead><tr>';
    html += `<th style="width:30px;"><input type="checkbox" data-registrar-select="${esc(registrar)}" title="Select all"></th>`;
    html += '<th>Domain</th><th>Status</th><th>Expires</th><th>Days Left</th><th>Auto-Renew</th>';
    html += '</tr></thead><tbody>';

    domains.forEach(dm => {
      const daysLeft = dm.days_until_expiry;
      const daysColor = daysLeft === null || daysLeft === undefined ? '#9ca3af' : (daysLeft <= 7 ? '#ef4444' : (daysLeft <= 30 ? '#f59e0b' : '#22c55e'));
      const renewColor = dm.auto_renew ? '#22c55e' : '#f59e0b';
      const renewLabel = dm.auto_renew ? 'ON' : 'OFF';
      const statusColor = (dm.status === 'ACTIVE' || dm.status === 'registered') ? '#22c55e' : '#f59e0b';

      html += '<tr>';
      html += `<td><input type="checkbox" class="dom-select" data-domain="${esc(dm.domain)}" data-registrar="${esc(dm.registrar)}" data-autorenew="${dm.auto_renew}"></td>`;
      html += `<td>${esc(dm.domain)}</td>`;
      html += `<td style="color:${statusColor}">${esc(dm.status || '')}</td>`;
      html += `<td>${esc(dm.expires || '?')}</td>`;
      html += `<td style="color:${daysColor}">${daysLeft !== null && daysLeft !== undefined ? esc(String(daysLeft)) + 'd' : '?'}</td>`;
      html += `<td><button class="auto-renew-toggle" data-ar-domain="${esc(dm.domain)}" data-ar-registrar="${esc(dm.registrar)}" data-ar-current="${dm.auto_renew}" style="background:none;border:1px solid ${renewColor};color:${renewColor};padding:2px 8px;border-radius:4px;cursor:pointer;font-size:12px;">${renewLabel}</button></td>`;
      html += '</tr>';
    });

    html += '</tbody></table>';
  }

  container.innerHTML = html;

  // --- Event Delegation ---
  bindEvents(byRegistrar);
}

function bindEvents(byRegistrar) {
  if (!container) return;

  // Select-all per registrar
  container.querySelectorAll('[data-registrar-select]').forEach(cb => {
    const registrar = cb.getAttribute('data-registrar-select');
    cb.addEventListener('change', () => {
      container.querySelectorAll('.dom-select').forEach(dcb => {
        if (dcb.dataset.registrar === registrar) dcb.checked = cb.checked;
      });
    });
  });

  // Auto-renew toggle buttons
  container.querySelectorAll('.auto-renew-toggle').forEach(btn => {
    btn.addEventListener('click', () => handleToggleAutoRenew(btn));
  });

  // Bulk disable
  const bulkBtn = container.querySelector('#bulk-disable-btn');
  if (bulkBtn) {
    bulkBtn.addEventListener('click', handleBulkDisable);
  }
}

async function handleToggleAutoRenew(btn) {
  const domain = btn.getAttribute('data-ar-domain');
  const registrar = btn.getAttribute('data-ar-registrar');
  const currentlyOn = btn.getAttribute('data-ar-current') === 'true';
  const enabled = !currentlyOn;

  btn.disabled = true;
  btn.textContent = '...';

  try {
    const result = await apiPost('/api/domains/auto-renew', { domain, registrar, enabled });
    if (result.success) {
      const newColor = enabled ? '#22c55e' : '#f59e0b';
      btn.textContent = enabled ? 'ON' : 'OFF';
      btn.style.color = newColor;
      btn.style.borderColor = newColor;
      btn.setAttribute('data-ar-current', String(enabled));
      btn.disabled = false;
      showToast(`Auto-renew ${enabled ? 'enabled' : 'disabled'} for ${domain}`, 'success');
    } else {
      showToast('Failed: ' + (result.message || 'Unknown error'), 'error');
      btn.textContent = currentlyOn ? 'ON' : 'OFF';
      btn.disabled = false;
    }
  } catch (err) {
    showToast('Error: ' + err.message, 'error');
    btn.textContent = currentlyOn ? 'ON' : 'OFF';
    btn.disabled = false;
  }
}

async function handleBulkDisable() {
  if (!container) return;
  const domains = [];
  container.querySelectorAll('.dom-select').forEach(cb => {
    if (cb.dataset.autorenew === 'true') {
      domains.push({ domain: cb.dataset.domain, registrar: cb.dataset.registrar });
    }
  });
  if (!domains.length) {
    showToast('No domains with auto-renew ON', 'warn');
    return;
  }

  const btn = container.querySelector('#bulk-disable-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = `Disabling ${domains.length} domains...`;
  }

  try {
    const result = await apiPost('/api/domains/bulk-auto-renew', {
      domains: domains.map(d => ({ domain: d.domain, registrar: d.registrar })),
      enabled: false,
    });
    showToast(
      `Disabled: ${result.success} succeeded, ${result.failed} failed`,
      result.failed ? 'warn' : 'success',
      5000
    );
    // Reload domain data
    fetchSlice('domains', '/api/domains').catch(() => {});
  } catch (err) {
    showToast('Bulk disable error: ' + err.message, 'error');
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Disable All Auto-Renew';
    }
  }
}

function statCardHtml(value, label, variant = '') {
  return `<div class="stat-card ${esc(variant)}"><div class="value">${esc(String(value))}</div><div class="label">${esc(label)}</div></div>`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
