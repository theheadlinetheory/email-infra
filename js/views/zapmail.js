/**
 * ZapMail view — domains by client, mailbox info, renewal alerts.
 */

import { store } from '../core/state.js';
import { fetchSlice } from '../core/api.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('zapmail', render));
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
    await fetchSlice('zapmail', '/api/zapmail');
  } catch (e) { /* handled */ }
}

function render() {
  if (!container) return;
  const data = store.get('zapmail');
  const loading = store.get('loading')?.zapmail;
  const error = store.get('errors')?.zapmail;

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading ZapMail data...</div>';
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
  header.innerHTML = `<h2>ZapMail</h2><p class="subtitle">${data.total_domains || 0} domains</p>`;
  container.appendChild(header);

  // Renewal alerts
  if (data.renewing_soon?.length > 0) {
    const alert = document.createElement('div');
    alert.className = 'alert-banner';
    alert.innerHTML = `<h3>Renewing Soon</h3>` +
      data.renewing_soon.map(d => `<div class="alert-item">${esc(d.domain)} — ${d.days_until_renewal || '?'} days</div>`).join('');
    container.appendChild(alert);
  }

  // Clients
  const clients = data.by_client || data.clients || {};
  for (const [clientName, domains] of Object.entries(clients)) {
    const card = document.createElement('div');
    card.className = `zm-client-card ${domains.some(d => d.renewing_soon) ? 'has-renewal' : ''}`;
    card.innerHTML = `
      <div class="zm-client-header">
        <h3>${esc(clientName)}</h3>
        <span class="zm-meta">${domains.length} domains</span>
      </div>
      <table class="zm-domain-table">
        <thead><tr><th>Domain</th><th>Status</th><th>Mailboxes</th><th>Created</th></tr></thead>
        <tbody>${domains.map(d => `
          <tr>
            <td>${esc(d.domain || '')}</td>
            <td><span class="badge badge-${d.status === 'ACTIVE' ? 'green' : 'yellow'}">${esc(d.status || 'unknown')}</span></td>
            <td>${d.mailbox_count ?? d.mailboxes?.length ?? '—'}</td>
            <td style="color:var(--text-muted);font-size:12px;">${esc((d.createdAt || '').slice(0, 10))}</td>
          </tr>
        `).join('')}</tbody>
      </table>
    `;
    container.appendChild(card);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
