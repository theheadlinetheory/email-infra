import { store } from '../core/state.js';
import { fetchSlice } from '../core/api.js';
import { statCard, statCardSkeleton } from '../components/stat-card.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('sync', render));
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
    await fetchSlice('sync', '/api/zapmail/sync');
  } catch (e) { /* handled by store */ }
}

function render() {
  if (!container) return;
  const data = store.get('sync');
  const loading = store.get('loading')?.sync;
  const error = store.get('errors')?.sync;

  if (loading && !data) {
    container.innerHTML = '';
    const stats = document.createElement('div');
    stats.className = 'summary-row';
    for (let i = 0; i < 4; i++) stats.appendChild(statCardSkeleton());
    container.appendChild(stats);
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
  header.innerHTML = `<h2>Sync Check</h2><p class="subtitle">ZapMail ↔ SmartLead domain alignment</p>`;
  container.appendChild(header);

  const stats = document.createElement('div');
  stats.className = 'summary-row';
  stats.appendChild(statCard({ value: data.total_checked || 0, label: 'Domains Checked' }));
  stats.appendChild(statCard({ value: (data.mismatches || []).length, label: 'Tag Mismatches', variant: (data.mismatches || []).length > 0 ? 'alert' : 'good' }));
  stats.appendChild(statCard({ value: data.zapmail_only_count || 0, label: 'ZapMail Only', variant: (data.zapmail_only_count || 0) > 0 ? 'warn' : '' }));
  stats.appendChild(statCard({ value: data.smartlead_only_count || 0, label: 'SmartLead Only', variant: (data.smartlead_only_count || 0) > 0 ? 'warn' : '' }));
  container.appendChild(stats);

  if ((data.mismatches || []).length > 0) {
    const title = document.createElement('h2');
    title.className = 'section-title';
    title.textContent = 'Tag Mismatches';
    container.appendChild(title);

    for (const m of data.mismatches) {
      const item = document.createElement('div');
      item.className = 'sync-item';
      item.innerHTML = `<span class="domain">${esc(m.domain)}</span> — ZapMail: <span class="mismatch">${esc(m.zapmail_tag)}</span> vs SmartLead: <span class="mismatch">${esc(m.smartlead_client)}</span>`;
      container.appendChild(item);
    }
  }

  const sections = [
    { title: 'In ZapMail but not SmartLead', items: data.zapmail_only || [], count: data.zapmail_only_count || 0 },
    { title: 'In SmartLead but not ZapMail', items: data.smartlead_only || [], count: data.smartlead_only_count || 0 },
  ];

  for (const section of sections) {
    if (section.count > 0) {
      const title = document.createElement('h2');
      title.className = 'section-title';
      title.textContent = `${section.title} (${section.count})`;
      container.appendChild(title);

      for (const domain of section.items) {
        const item = document.createElement('div');
        item.className = 'sync-item';
        item.innerHTML = `<span class="domain">${esc(domain)}</span>`;
        container.appendChild(item);
      }
      if (section.count > 20) {
        const more = document.createElement('div');
        more.className = 'sync-item';
        more.style.color = 'var(--text-muted)';
        more.textContent = `...and ${section.count - 20} more`;
        container.appendChild(more);
      }
    }
  }

  if ((data.mismatches || []).length === 0 && (data.zapmail_only_count || 0) === 0 && (data.smartlead_only_count || 0) === 0) {
    const ok = document.createElement('div');
    ok.className = 'sync-item';
    ok.style.color = 'var(--accent)';
    ok.textContent = 'Everything in sync!';
    container.appendChild(ok);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
