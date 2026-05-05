/**
 * Domains view — registrar domains, expiry tracking, auto-renew management.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost } from '../core/api.js';
import { dataTable } from '../components/data-table.js';
import { statCard } from '../components/stat-card.js';
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
  } catch (e) { /* handled */ }
}

function render() {
  if (!container) return;
  const data = store.get('domains');
  const loading = store.get('loading')?.domains;
  const error = store.get('errors')?.domains;

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading domains...</div>';
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
  header.innerHTML = `<h2>Domains</h2><p class="subtitle">${data.total_domains || 0} registered domains</p>`;
  container.appendChild(header);

  // Stats
  const stats = document.createElement('div');
  stats.className = 'summary-row';
  stats.appendChild(statCard({ value: data.total_domains || 0, label: 'Total Domains' }));
  stats.appendChild(statCard({ value: data.expiring_soon || 0, label: 'Expiring Soon', variant: data.expiring_soon > 0 ? 'alert' : '' }));
  stats.appendChild(statCard({ value: data.no_auto_renew_30d || 0, label: 'No Auto-Renew (30d)', variant: data.no_auto_renew_30d > 0 ? 'warn' : '' }));
  container.appendChild(stats);

  // Domains by registrar
  for (const [registrar, domains] of Object.entries(data.by_registrar || {})) {
    const title = document.createElement('h2');
    title.className = 'section-title';
    title.textContent = `${registrar} (${domains.length})`;
    container.appendChild(title);

    const table = dataTable({
      columns: [
        { key: 'domain', label: 'Domain', sortable: true },
        { key: 'status', label: 'Status' },
        { key: 'expires', label: 'Expires', sortable: true },
        { key: 'days_until_expiry', label: 'Days Left', sortable: true, render: (row) => {
          const days = row.days_until_expiry;
          if (days == null) return '—';
          const color = days <= 14 ? 'var(--red)' : days <= 30 ? 'var(--yellow)' : 'var(--text-secondary)';
          return `<span style="color:${color};font-weight:600">${days}</span>`;
        }},
        { key: 'auto_renew', label: 'Auto-Renew', render: (row) => {
          return row.auto_renew
            ? '<span class="badge badge-green">ON</span>'
            : '<span class="badge badge-red">OFF</span>';
        }},
      ],
      rows: domains,
    });
    container.appendChild(table);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
