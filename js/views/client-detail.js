/**
 * Client detail view — accounts table, health stats, actions.
 * Loaded via #client/{id} route.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost } from '../core/api.js';
import { dataTable, dataTableSkeleton } from '../components/data-table.js';
import { statCard } from '../components/stat-card.js';
import { showToast } from '../components/toast.js';

let container = null;
let unsubs = [];
let clientId = null;

export function mount(el) {
  container = el;
  clientId = location.hash.split('/')[1] || null;
  if (!clientId) {
    el.innerHTML = '<p style="color:var(--text-muted);padding:24px;">No client selected</p>';
    return;
  }

  const key = `clientAccounts_${clientId}`;
  unsubs.push(store.subscribe(key, render));
  unsubs.push(store.subscribe('loading', render));
  unsubs.push(store.subscribe('errors', render));
  load();
}

export function destroy() {
  unsubs.forEach(fn => fn());
  unsubs = [];
  container = null;
  clientId = null;
}

async function load() {
  if (!clientId) return;
  const key = `clientAccounts_${clientId}`;
  try {
    await fetchSlice(key, `/api/client/${clientId}/accounts`);
  } catch (e) { /* handled by store */ }
}

function render() {
  if (!container || !clientId) return;
  const key = `clientAccounts_${clientId}`;
  const data = store.get(key);
  const loading = store.get('loading')?.[key];
  const error = store.get('errors')?.[key];

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading client data...</div>';
    return;
  }
  if (error && !data) {
    container.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'error-card';
    card.innerHTML = `<div class="error-msg">${esc(error)}</div><button class="retry-btn">Retry</button>`;
    card.querySelector('.retry-btn').addEventListener('click', load);
    container.appendChild(card);
    return;
  }
  if (!data) return;

  container.innerHTML = '';

  // Header
  const header = document.createElement('div');
  header.className = 'page-header';
  header.innerHTML = `
    <h2>${esc(data.client_name || 'Client')}</h2>
    <p class="subtitle">${data.accounts?.length || 0} accounts</p>
  `;
  container.appendChild(header);

  // Stats
  const stats = document.createElement('div');
  stats.className = 'summary-row';
  stats.appendChild(statCard({ value: data.accounts?.length || 0, label: 'Total Accounts' }));
  stats.appendChild(statCard({
    value: data.flagged_inbox_count || 0,
    label: 'Flagged',
    variant: data.flagged_inbox_count > 0 ? 'alert' : 'good',
  }));
  stats.appendChild(statCard({
    value: data.replacement_domains_needed || 0,
    label: 'Replacements Needed',
    variant: data.replacement_domains_needed > 0 ? 'warn' : '',
  }));
  container.appendChild(stats);

  // Accounts table
  if (data.accounts?.length > 0) {
    const title = document.createElement('h2');
    title.className = 'section-title';
    title.textContent = 'Accounts';
    container.appendChild(title);

    const table = dataTable({
      columns: [
        { key: 'from_email', label: 'Email', sortable: true },
        { key: 'health_score', label: 'Health', sortable: true, render: (row) => {
          const score = row.health_score ?? row.health?.score;
          if (score == null) return '<span style="color:var(--text-muted)">—</span>';
          const color = score >= 90 ? 'var(--green)' : score >= 75 ? 'var(--yellow)' : 'var(--red)';
          return `<span style="color:${color};font-weight:600">${Math.round(score)}%</span>`;
        }},
        { key: 'warmup_status', label: 'Status', render: (row) => {
          const wd = row.warmup_details || {};
          const status = wd.warmup_status || 'unknown';
          return `<span class="badge badge-${status === 'completed' ? 'green' : status === 'active' ? 'yellow' : 'red'}">${esc(status)}</span>`;
        }},
        { key: 'campaigns', label: 'Campaigns', render: (row) => {
          return String(row.campaign_count || 0);
        }},
      ],
      rows: data.accounts,
      emptyMessage: 'No accounts found',
    });
    container.appendChild(table);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
