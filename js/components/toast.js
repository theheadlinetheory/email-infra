/**
 * Toast notification component.
 * Shows success/error/info messages with auto-dismiss.
 */

let container = null;

function ensureContainer() {
  if (container) return container;
  container = document.createElement('div');
  container.className = 'toast-container';
  document.body.appendChild(container);
  return container;
}

export function showToast(message, type = 'info', duration = 4000) {
  const c = ensureContainer();
  const icons = { success: '✓', error: '✕', info: 'ℹ' };

  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span class="toast-msg">${esc(message)}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
  `;
  c.appendChild(el);

  if (duration > 0) {
    setTimeout(() => {
      el.style.animation = 'toast-out 0.3s ease-out forwards';
      setTimeout(() => el.remove(), 300);
    }, duration);
  }

  return el;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
