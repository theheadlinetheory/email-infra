/**
 * Pipelines view — pipeline cards with step progress pills.
 */

import { store } from '../core/state.js';
import { fetchSlice } from '../core/api.js';

let container = null;
let unsubs = [];

export function mount(el) {
  container = el;
  unsubs.push(store.subscribe('pipelines', render));
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
    await fetchSlice('pipelines', '/api/pipeline/active');
  } catch (e) { /* handled */ }
}

function render() {
  if (!container) return;
  const data = store.get('pipelines');
  const loading = store.get('loading')?.pipelines;
  const error = store.get('errors')?.pipelines;

  if (loading && !data) {
    container.innerHTML = '<div class="loading"><span class="spinner"></span> Loading pipelines...</div>';
    return;
  }
  if (error && !data) {
    container.innerHTML = `<div class="error-card"><div class="error-msg">${esc(error)}</div><button class="retry-btn">Retry</button></div>`;
    container.querySelector('.retry-btn')?.addEventListener('click', load);
    return;
  }

  const pipelines = data?.pipelines || [];
  container.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'page-header';
  header.innerHTML = `<h2>Pipelines</h2><p class="subtitle">${pipelines.length} pipeline${pipelines.length !== 1 ? 's' : ''}</p>`;
  container.appendChild(header);

  if (pipelines.length === 0) {
    container.innerHTML += '<p style="color:var(--text-muted);padding:16px;">No active pipelines</p>';
    return;
  }

  for (const p of pipelines) {
    container.appendChild(pipelineCard(p));
  }
}

function pipelineCard(p) {
  const card = document.createElement('div');
  card.className = 'client-card';
  card.style.marginBottom = '12px';

  const statusColor = p.status === 'complete' ? 'var(--green)' :
                      p.status === 'error' ? 'var(--red)' :
                      p.status === 'running' ? 'var(--yellow)' : 'var(--text-muted)';

  card.innerHTML = `
    <div class="cc-header">
      <span class="cc-name">${esc(p.client_name || p.id)}</span>
      <span class="badge" style="background:${statusColor}20;color:${statusColor}">${esc(p.status)}</span>
    </div>
    <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">${esc(p.type)} — ${p.domains?.length || 0} domains</div>
    <div class="pill-stepper">${buildStepPills(p)}</div>
    ${p.errors?.length ? `<div style="font-size:12px;color:var(--red);margin-top:8px;">${esc(p.errors[p.errors.length - 1])}</div>` : ''}
  `;

  return card;
}

function buildStepPills(p) {
  const steps = p.steps || [];
  const currentIdx = steps.indexOf(p.current_step);
  let html = '';

  for (let i = 0; i < steps.length; i++) {
    if (i > 0) html += `<div class="pill-connector ${i <= currentIdx ? 'done' : 'pending'}"></div>`;
    const cls = i < currentIdx ? 'completed' :
                i === currentIdx ? (p.status === 'error' ? 'failed' : 'running') : 'pending';
    const icon = i < currentIdx ? '✓' : i === currentIdx ? (p.status === 'error' ? '✕' : '◉') : '';
    html += `<div class="pill-step ${cls}">${icon ? `<span class="pill-icon">${icon}</span>` : ''}${esc(steps[i])}</div>`;
  }
  return html;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
