/**
 * Hash-based tab routing.
 * Maps #overview, #pipelines, #zapmail etc. to view modules.
 */

import { store } from './state.js';

const VIEW_MAP = {
  overview: () => import('../views/overview.js'),
  pipelines: () => import('../views/pipelines.js'),
  zapmail: () => import('../views/zapmail.js'),
  domains: () => import('../views/domains.js'),
  acquisition: () => import('../views/acquisition.js'),
  client: () => import('../views/client-detail.js'),
};

const VALID_VIEWS = Object.keys(VIEW_MAP);

let currentModule = null;
let container = null;

export function initRouter(el) {
  container = el;

  window.addEventListener('hashchange', () => {
    const view = location.hash.replace('#', '').split('/')[0] || 'overview';
    navigate(view);
  });

  store.subscribe('currentView', () => {
    renderCurrentView();
  });
}

export function navigate(view) {
  if (!VALID_VIEWS.includes(view)) view = 'overview';
  store.setView(view);
}

async function renderCurrentView() {
  const view = store.get('currentView');
  if (!container) return;

  // Destroy previous view
  if (currentModule?.destroy) {
    currentModule.destroy();
  }

  const loader = VIEW_MAP[view];
  if (!loader) {
    container.innerHTML = '<p style="color:var(--text-muted);padding:24px;">View not found</p>';
    return;
  }

  try {
    const mod = await loader();
    currentModule = mod;
    if (mod.mount) {
      mod.mount(container);
    }
  } catch (e) {
    container.innerHTML = `<p style="color:var(--red);padding:24px;">Failed to load view: ${e.message}</p>`;
  }
}

export function getViewParam() {
  const parts = location.hash.replace('#', '').split('/');
  return parts[1] || null;
}
