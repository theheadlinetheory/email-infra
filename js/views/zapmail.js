/**
 * ZapMail view — domains by client tag, mailbox info, renewal alerts, wallet balance.
 * Collapsible client cards with cancel-subscription support.
 */

import { store } from '../core/state.js';
import { fetchSlice, apiPost, apiGet } from '../core/api.js';
import { statCard } from '../components/stat-card.js';
import { showToast } from '../components/toast.js';

let container = null;
let unsubs = [];
let walletBalance = null;

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
  walletBalance = null;
}

async function load() {
  try {
    await Promise.all([
      fetchSlice('zapmail', '/api/zapmail'),
      loadWallet(),
    ]);
  } catch (e) { /* handled by store */ }
}

async function loadWallet() {
  try {
    const resp = await apiGet('/api/wallet');
    const balance = resp?.data?.balance ?? resp?.balance ?? null;
    walletBalance = balance !== null ? parseFloat(balance) : null;
    render();
  } catch (e) {
    walletBalance = null;
  }
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

  const clients = data.clients || [];
  const totalDomains = data.total_domains || 0;
  const totalMailboxes = data.total_mailboxes || 0;
  const renewingSoon = clients.reduce((n, c) => n + (c.renewing_soon || 0), 0);

  let html = '';

  // --- Summary Row ---
  html += '<div class="summary-row" id="zm-summary-row">';
  html += statCardHtml(totalDomains, 'Total Domains', 'good');
  html += statCardHtml(totalMailboxes, 'Total Mailboxes', 'good');
  html += statCardHtml(clients.length, 'Client Tags', 'good');
  html += statCardHtml(renewingSoon, 'Renewing in 3 days', renewingSoon > 0 ? 'alert' : 'good');
  html += '</div>';

  // --- Wallet Balance ---
  if (walletBalance !== null) {
    const wColor = walletBalance < 50 ? '#ef4444' : walletBalance < 150 ? '#f59e0b' : '#22c55e';
    html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;padding:12px 16px;background:var(--bg-raised);border:1px solid var(--border);border-radius:var(--radius);font-size:14px;">
      <span style="color:var(--text-secondary);">ZapMail Wallet:</span>
      <span style="color:${wColor};font-weight:700;font-size:18px;">$${walletBalance.toFixed(2)}</span>
    </div>`;
  }

  // --- Client Cards ---
  html += clients.map(cl => {
    const cardId = 'zm-' + cl.name.replace(/[^a-zA-Z0-9]/g, '_');
    return `
    <div class="zm-client-card ${cl.renewing_soon > 0 ? 'has-renewal' : ''}">
      <div class="zm-client-header" data-toggle="${cardId}">
        <h3>${esc(cl.name)}</h3>
        <span class="zm-meta">${esc(String(cl.domains))} domains, ${esc(String(cl.mailboxes))} mailboxes ${cl.renewing_soon > 0 ? '<span class="badge badge-red">' + esc(String(cl.renewing_soon)) + ' renewing soon</span>' : ''}</span>
      </div>
      <div id="${cardId}" style="display:none;">
        <div class="cancel-controls" style="display:flex;align-items:center;gap:12px;padding:8px 0;">
          <button class="cancel-btn" data-cancel-card="${cardId}" disabled>Cancel Selected Domains</button>
          <span class="cancel-status" data-cancel-status="${cardId}"></span>
        </div>
        <table class="zm-domain-table">
          <thead><tr>
            <th><input type="checkbox" data-select-all="${cardId}"></th>
            <th>Domain</th>
            <th>Mailboxes</th>
            <th>Created</th>
            <th>Next Renewal</th>
            <th>Status</th>
          </tr></thead>
          <tbody>
          ${(cl.domain_list || []).map(dm => {
            const renewBadge = dm.days_until_renewal !== null && dm.days_until_renewal <= 3
              ? '<span class="badge badge-red">' + esc(String(dm.days_until_renewal)) + 'd</span>'
              : (dm.days_until_renewal !== null ? esc(String(dm.days_until_renewal)) + 'd' : '');
            return `<tr>
              <td><input type="checkbox" class="zm-check-${cardId}" value="${esc(String(dm.id))}"></td>
              <td>${esc(dm.domain)}</td>
              <td>${esc(String(dm.mailbox_count))}</td>
              <td>${esc(dm.created || '')}</td>
              <td>${esc(dm.next_renewal || '?')} ${renewBadge}</td>
              <td style="color:${dm.status === 'ACTIVE' ? '#22c55e' : '#ef4444'}">${esc(dm.status || '')}</td>
            </tr>`;
          }).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
  }).join('');

  container.innerHTML = html;

  // --- Event Delegation ---
  bindEvents(clients);
}

function bindEvents(clients) {
  if (!container) return;

  // Toggle collapsible client sections
  container.querySelectorAll('[data-toggle]').forEach(header => {
    header.addEventListener('click', () => {
      const targetId = header.getAttribute('data-toggle');
      const target = document.getElementById(targetId);
      if (target) target.style.display = target.style.display === 'none' ? 'block' : 'none';
    });
  });

  // Select-all checkboxes per client
  container.querySelectorAll('[data-select-all]').forEach(cb => {
    const cardId = cb.getAttribute('data-select-all');
    cb.addEventListener('change', () => {
      container.querySelectorAll('.zm-check-' + cardId).forEach(c => { c.checked = cb.checked; });
      updateCancelBtn(cardId);
    });
  });

  // Individual checkboxes update cancel button state
  clients.forEach(cl => {
    const cardId = 'zm-' + cl.name.replace(/[^a-zA-Z0-9]/g, '_');
    container.querySelectorAll('.zm-check-' + cardId).forEach(cb => {
      cb.addEventListener('change', () => updateCancelBtn(cardId));
    });
  });

  // Cancel buttons
  container.querySelectorAll('[data-cancel-card]').forEach(btn => {
    const cardId = btn.getAttribute('data-cancel-card');
    btn.addEventListener('click', () => cancelSelectedDomains(cardId));
  });
}

function updateCancelBtn(cardId) {
  if (!container) return;
  const selected = container.querySelectorAll('.zm-check-' + cardId + ':checked').length;
  const btn = container.querySelector(`[data-cancel-card="${cardId}"]`);
  if (btn) btn.disabled = selected === 0;
}

async function cancelSelectedDomains(cardId) {
  if (!container) return;
  const domainIds = Array.from(container.querySelectorAll('.zm-check-' + cardId + ':checked')).map(cb => cb.value);
  if (!domainIds.length) return;

  if (!confirm('Cancel ' + domainIds.length + ' domain(s) from ZapMail? This stops billing but domains stay on Spaceship.')) return;

  const btn = container.querySelector(`[data-cancel-card="${cardId}"]`);
  const status = container.querySelector(`[data-cancel-status="${cardId}"]`);
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Cancelling...';

  try {
    const result = await apiPost('/api/zapmail/cancel', { domain_ids: domainIds });
    if (result.error) {
      if (status) status.textContent = 'Error: ' + String(result.error).substring(0, 100);
      showToast('Cancel failed: ' + String(result.error).substring(0, 80), 'error');
    } else {
      if (status) status.textContent = 'Cancelled! Refreshing...';
      showToast('Domains cancelled successfully', 'success');
      setTimeout(() => {
        fetchSlice('zapmail', '/api/zapmail').catch(() => {});
      }, 1500);
    }
  } catch (err) {
    if (status) status.textContent = 'Error: ' + err.message;
    showToast('Cancel error: ' + err.message, 'error');
  }
}

/** Inline stat card HTML (matches old statCard() global function signature) */
function statCardHtml(value, label, variant = '') {
  return `<div class="stat-card ${esc(variant)}"><div class="value">${esc(String(value))}</div><div class="label">${esc(label)}</div></div>`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
