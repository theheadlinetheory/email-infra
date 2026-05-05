/**
 * Overview view — main dashboard with client cards, health stats, alerts.
 * Three-state rendering: loading → error → data.
 */

import { store } from '../core/state.js';
import { fetchSlice } from '../core/api.js';
import { statCard, statCardSkeleton } from '../components/stat-card.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('overview', render));
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
    await fetchSlice('overview', '/api/overview');
  } catch (e) { /* error handled by store */ }
}

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
  for (let i = 0; i < 4; i++) {
    const card = document.createElement('div');
    card.className = 'skeleton skeleton-card';
    card.style.height = '120px';
    grid.appendChild(card);
  }
  el.appendChild(grid);
}

function renderError(el, error) {
  const card = document.createElement('div');
  card.className = 'error-card';
  card.innerHTML = `
    <div class="error-msg">${esc(error)}</div>
    <button class="retry-btn" onclick="this.disabled=true;">Retry</button>
  `;
  card.querySelector('.retry-btn').addEventListener('click', load);
  el.appendChild(card);
}

function renderData(el, data, meta) {
  // Stale badge
  if (meta?.cached) {
    const badge = document.createElement('div');
    badge.className = 'stale-badge';
    badge.textContent = `Updated ${meta.stale_seconds}s ago`;
    badge.style.marginBottom = '12px';
    badge.addEventListener('click', load);
    el.appendChild(badge);
  }

  // Stats row
  const stats = document.createElement('div');
  stats.className = 'summary-row';
  stats.appendChild(statCard({ value: data.total_accounts || 0, label: 'Total Accounts' }));
  stats.appendChild(statCard({ value: data.in_campaign || 0, label: 'In Campaign' }));
  stats.appendChild(statCard({
    value: data.smtp_failures || 0,
    label: 'SMTP Failures',
    variant: (data.smtp_failures || 0) > 0 ? 'alert' : 'good',
  }));
  stats.appendChild(statCard({
    value: data.imap_failures || 0,
    label: 'IMAP Failures',
    variant: (data.imap_failures || 0) > 0 ? 'alert' : 'good',
  }));
  stats.appendChild(statCard({
    value: data.unassigned || 0,
    label: 'Unassigned',
    variant: (data.unassigned || 0) > 0 ? 'warn' : '',
  }));
  el.appendChild(stats);

  // Blocked alerts
  if (data.blocked?.length > 0) {
    const alert = document.createElement('div');
    alert.className = 'alert-banner';
    alert.innerHTML = `<h3>Blocked Accounts</h3>` +
      data.blocked.map(b => `<div class="alert-item">${esc(b.email)} — ${esc(b.reason || 'blocked')}</div>`).join('');
    el.appendChild(alert);
  }

  // Client cards
  const title = document.createElement('h2');
  title.className = 'section-title';
  title.textContent = 'Clients';
  el.appendChild(title);

  const grid = document.createElement('div');
  grid.className = 'clients-grid';
  for (const client of (data.clients || [])) {
    grid.appendChild(clientCard(client));
  }
  el.appendChild(grid);
}

function clientCard(client) {
  const card = document.createElement('div');
  card.className = `client-card ${client.flagged_inbox_count > 0 ? 'has-alert' : ''}`;

  const healthPct = client.avg_health != null ? `${Math.round(client.avg_health)}%` : '—';
  const healthColor = (client.avg_health || 0) >= 90 ? 'var(--green)' :
                      (client.avg_health || 0) >= 75 ? 'var(--yellow)' : 'var(--red)';

  card.innerHTML = `
    <div class="cc-header">
      <span class="cc-name">${esc(client.name || client.client_name || '')}</span>
      <span class="cc-count" style="color:${healthColor}">${healthPct}</span>
    </div>
    <div class="cc-stats">
      <div class="cc-stat"><span class="label">Accounts</span><span>${client.account_count || 0}</span></div>
      <div class="cc-stat"><span class="label">Domains</span><span>${client.domain_count || 0}</span></div>
      <div class="cc-stat"><span class="label">In Campaign</span><span>${client.in_campaign || 0}</span></div>
      <div class="cc-stat"><span class="label">Flagged</span><span style="color:${client.flagged_inbox_count ? 'var(--red)' : 'inherit'}">${client.flagged_inbox_count || 0}</span></div>
    </div>
  `;

  card.addEventListener('click', () => {
    location.hash = `client/${client.id}`;
  });

  return card;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
