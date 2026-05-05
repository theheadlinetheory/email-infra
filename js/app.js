/**
 * App entry point — init auth, mount router, apply theme, load first view.
 */

import { store } from './core/state.js';
import { initAuth, login, logout, getCurrentUser } from './core/auth.js';
import { initRouter, navigate } from './core/router.js';
import { showToast } from './components/toast.js';

document.addEventListener('DOMContentLoaded', () => {
  // Apply saved theme
  const theme = store.get('theme');
  document.documentElement.setAttribute('data-theme', theme);

  // Init auth
  initAuth();

  // Wait for auth before rendering
  store.subscribe('authReady', (ready) => {
    if (!ready) return;
    const user = store.get('user');
    if (user) {
      renderApp();
    } else {
      renderLogin();
    }
  });

  store.subscribe('user', (user) => {
    if (user) {
      renderApp();
    } else {
      renderLogin();
    }
  });

  // If Firebase not loaded, render app directly (password auth fallback)
  if (!window.firebase) {
    renderApp();
  }
});

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
        <h1>THT <span>Infrastructure</span></h1>
        <nav class="topbar-nav" id="nav-tabs"></nav>
      </div>
      <div class="topbar-right">
        <span id="user-name"></span>
        <button class="theme-toggle" id="theme-toggle" title="Toggle theme"></button>
        <button id="logout-btn">Logout</button>
      </div>
    </div>
    <div class="container" id="view-container"></div>
  `;

  // Nav tabs
  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'pipelines', label: 'Pipelines' },
    { id: 'zapmail', label: 'ZapMail' },
    { id: 'domains', label: 'Domains' },
    { id: 'acquisition', label: 'Acquisition' },
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

  // User info
  const user = getCurrentUser();
  if (user) {
    document.getElementById('user-name').textContent = user.name || user.email || '';
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
}

function updateActiveTab() {
  const view = store.get('currentView');
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
}
