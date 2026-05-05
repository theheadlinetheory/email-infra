/**
 * Stat card component.
 * Renders a value + label card with optional color variant.
 */

export function statCard({ value, label, variant = '' }) {
  const el = document.createElement('div');
  el.className = `stat-card ${variant}`;
  el.innerHTML = `
    <div class="value">${esc(String(value))}</div>
    <div class="label">${esc(label)}</div>
  `;
  return el;
}

export function statCardSkeleton() {
  const el = document.createElement('div');
  el.className = 'stat-card';
  el.innerHTML = `
    <div class="skeleton skeleton-line" style="width:60px;height:26px;margin-bottom:6px;"></div>
    <div class="skeleton skeleton-line" style="width:80px;height:11px;"></div>
  `;
  return el;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
