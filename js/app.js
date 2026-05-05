/**
 * App entry point — init auth, mount router, apply theme, load first view.
 */

import { store } from './core/state.js';
import { initAuth, login, logout, getCurrentUser } from './core/auth.js';
import { initRouter, navigate } from './core/router.js';
import { apiGet } from './core/api.js';
import { showToast } from './components/toast.js';

document.addEventListener('DOMContentLoaded', () => {
  // Apply saved theme
  const theme = store.get('theme');
  document.documentElement.setAttribute('data-theme', theme);

  // Init auth
  initAuth();

  // Wait for auth before rendering
  store.subscribe('authReady', async (ready) => {
    if (!ready) return;
    const user = store.get('user');
    if (user) {
      renderApp();
    } else if (await hasPasswordAuth()) {
      renderApp();
    } else {
      renderLogin();
    }
  });

  store.subscribe('user', (user) => {
    if (user) {
      renderApp();
    }
  });

  // If Firebase not loaded, render app directly (password auth fallback)
  if (!window.firebase) {
    renderApp();
  }
});

async function hasPasswordAuth() {
  try {
    const resp = await fetch('/api/auth-check');
    return resp.ok;
  } catch { return false; }
}

function renderLogin() {
  const app = document.getElementById('app');
  if (!app) return;

  app.innerHTML = `
    <div class="login-screen">
      <div class="login-box">
        <h2>THT Infrastructure</h2>
        <p class="subtitle">Sign in with your THT account</p>
        <input type="email" id="login-email" placeholder="Email" autocomplete="email">
        <input type="password" id="login-password" placeholder="Password" autocomplete="current-password">
        <button class="login-btn" id="login-btn">Sign In</button>
        <div class="login-error" id="login-error"></div>
      </div>
    </div>
  `;

  const btn = document.getElementById('login-btn');
  const emailInput = document.getElementById('login-email');
  const passInput = document.getElementById('login-password');
  const errEl = document.getElementById('login-error');

  async function handleLogin() {
    const email = emailInput.value.trim();
    const pass = passInput.value;
    if (!email || !pass) {
      errEl.textContent = 'Please fill in all fields';
      errEl.style.display = 'block';
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Signing in...';
    try {
      await login(email, pass);
    } catch (e) {
      errEl.textContent = e.message?.replace('Firebase: ', '') || 'Login failed';
      errEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Sign In';
    }
  }

  btn.addEventListener('click', handleLogin);
  passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleLogin(); });
}

function renderApp() {
  const app = document.getElementById('app');
  if (!app) return;

  app.innerHTML = `
    <div class="topbar">
      <div class="topbar-left">
        <h1><span>THT</span> Infrastructure</h1>
        <div class="mode-switcher">
          <button class="mode-btn active" id="mode-fulfillment">Clients</button>
          <button class="mode-btn" id="mode-acquisition">Acquisition</button>
        </div>
        <nav class="topbar-nav" id="nav-tabs"></nav>
        <div id="inventory-badges" style="display:flex;gap:6px;align-items:center;margin-left:8px;">
          <span id="inv-client" class="badge badge-green" style="font-size:11px;cursor:default;"></span>
          <span id="inv-acq" class="badge badge-green" style="font-size:11px;cursor:default;"></span>
        </div>
      </div>
      <div class="topbar-right">
        <span id="wallet-balance" style="color:var(--accent);font-weight:600;"></span>
        <span id="pipeline-badge" style="display:none;background:var(--purple);color:#fff;padding:3px 10px;border-radius:6px;font-size:11px;font-family:var(--font-mono);"></span>
        <span id="last-updated" style="font-size:11px;color:var(--text-muted);"></span>
        <button class="sync-btn" id="sync-btn">Sync</button>
        <button class="theme-toggle" id="theme-toggle" title="Toggle theme"></button>
        <button id="logout-btn">Logout</button>
      </div>
    </div>
    <div class="container" id="view-container"></div>
  `;

  // Nav tabs
  const tabs = [
    { id: 'overview', label: 'SmartLead' },
    { id: 'zapmail', label: 'ZapMail' },
    { id: 'domains', label: 'Domains' },
    { id: 'pipelines', label: 'Pipelines' },
    { id: 'sync', label: 'Sync Check' },
  ];

  const nav = document.getElementById('nav-tabs');
  for (const tab of tabs) {
    const btn = document.createElement('button');
    btn.className = 'nav-tab';
    btn.textContent = tab.label;
    btn.dataset.view = tab.id;
    btn.addEventListener('click', () => navigate(tab.id));
    nav.appendChild(btn);
  }

  // Mode switcher
  const modeFulfillment = document.getElementById('mode-fulfillment');
  const modeAcquisition = document.getElementById('mode-acquisition');
  store.set('mode', 'fulfillment');

  modeFulfillment.addEventListener('click', () => {
    store.set('mode', 'fulfillment');
    modeFulfillment.classList.add('active');
    modeAcquisition.classList.remove('active');
    if (store.get('currentView') === 'acquisition') navigate('overview');
  });
  modeAcquisition.addEventListener('click', () => {
    store.set('mode', 'acquisition');
    modeAcquisition.classList.add('active');
    modeFulfillment.classList.remove('active');
    navigate('acquisition');
  });

  // Highlight active tab
  store.subscribe('currentView', () => updateActiveTab());
  updateActiveTab();

  // Theme toggle
  const themeBtn = document.getElementById('theme-toggle');
  themeBtn.textContent = store.get('theme') === 'dark' ? '☀' : '☾';
  themeBtn.addEventListener('click', () => {
    const next = store.get('theme') === 'dark' ? 'light' : 'dark';
    store.setTheme(next);
    themeBtn.textContent = next === 'dark' ? '☀' : '☾';
  });

  // Sync button
  document.getElementById('sync-btn').addEventListener('click', () => {
    store.set('overview', null);
    store.set('zapmail', null);
    store.set('domains', null);
    store.set('sync', null);
    store.set('acquisition', null);
    const view = store.get('currentView') || 'overview';
    navigate(view);
    showToast('Refreshing data...', 'info', 2000);
  });

  // User info
  const user = getCurrentUser();
  if (user) {
    document.getElementById('user-name')?.textContent || '';
  }

  // Logout
  document.getElementById('logout-btn')?.addEventListener('click', () => {
    logout();
  });

  // Init router
  const viewContainer = document.getElementById('view-container');
  initRouter(viewContainer);
  const view = location.hash.replace('#', '').split('/')[0] || 'overview';
  navigate(view);

  // Load topbar data
  loadTopbarData();
}

function updateActiveTab() {
  const view = store.get('currentView');
  const match = view === 'client' ? 'overview' : view;
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === match);
  });
}

async function loadTopbarData() {
  // Wallet balance
  try {
    const walletData = await apiGet('/api/wallet');
    const balance = walletData?.data?.balance || walletData?.balance;
    const el = document.getElementById('wallet-balance');
    if (el && balance != null) {
      const num = parseFloat(balance);
      el.textContent = '$' + (isNaN(num) ? '?' : num.toFixed(2));
      el.style.color = num < 50 ? '#ef4444' : num < 150 ? '#f59e0b' : '#22c55e';
    }
  } catch (e) { /* non-critical */ }

  // Domain inventory badges
  try {
    const inv = await apiGet('/api/domain-inventory');
    const invData = inv?.data || inv;
    const clientEl = document.getElementById('inv-client');
    const acqEl = document.getElementById('inv-acq');
    if (clientEl && invData?.client_available != null) {
      clientEl.textContent = `${invData.client_available} client`;
      clientEl.className = `badge ${invData.client_available >= 20 ? 'badge-green' : 'badge-red'}`;
    }
    if (acqEl && invData?.acquisition_available != null) {
      acqEl.textContent = `${invData.acquisition_available} acq`;
      acqEl.className = `badge ${invData.acquisition_available >= 20 ? 'badge-green' : 'badge-red'}`;
    }
  } catch (e) { /* non-critical */ }

  // Pipeline badge
  try {
    const pipelines = await apiGet('/api/pipeline/active');
    const pData = pipelines?.data || pipelines;
    const running = Array.isArray(pData) ? pData.filter(p => p.status === 'running').length : 0;
    const badge = document.getElementById('pipeline-badge');
    if (badge) {
      if (running > 0) {
        badge.textContent = `${running} pipeline${running > 1 ? 's' : ''}`;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
      }
    }
  } catch (e) { /* non-critical */ }

  // Last updated timestamp
  const updated = document.getElementById('last-updated');
  if (updated) {
    updated.textContent = new Date().toLocaleTimeString();
  }
}
