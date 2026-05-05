/**
 * Acquisition view — groups, rotation status, campaign assignment.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost } from '../core/api.js';
import { showToast } from '../components/toast.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('acquisition', render));
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
    await fetchSlice('acquisition', '/api/acquisition');
  } catch (e) { /* handled */ }
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

  container.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'page-header';
  header.innerHTML = `<h2>Acquisition</h2>
    <p class="subtitle">${data.total_accounts || 0} accounts — ${data.total_daily_capacity || 0}/day capacity</p>`;
  container.appendChild(header);

  // Groups
  for (const group of (data.groups || [])) {
    const card = document.createElement('div');
    card.className = 'client-card';
    card.style.marginBottom = '12px';

    const healthColor = (group.avg_health || 0) >= 90 ? 'var(--green)' :
                        (group.avg_health || 0) >= 75 ? 'var(--yellow)' : 'var(--red)';

    card.innerHTML = `
      <div class="cc-header">
        <span class="cc-name">${esc(group.name || group.group_name || '')}</span>
        <span style="color:${healthColor};font-weight:600">${Math.round(group.avg_health || 0)}%</span>
      </div>
      <div class="cc-stats">
        <div class="cc-stat"><span class="label">Accounts</span><span>${group.account_count || 0}</span></div>
        <div class="cc-stat"><span class="label">Capacity</span><span>${group.daily_capacity || 0}/day</span></div>
        <div class="cc-stat"><span class="label">In Campaign</span><span>${group.in_campaign || 0}</span></div>
        <div class="cc-stat"><span class="label">Warmup</span><span>${group.in_warmup || 0}</span></div>
      </div>
    `;
    container.appendChild(card);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
