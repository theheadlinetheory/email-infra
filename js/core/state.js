/**
 * Reactive state store — subscribe/notify pattern.
 * Views subscribe to state slices and re-render when data changes.
 * All mutations go through store.* methods.
 */

const state = {
  currentView: location.hash.replace('#', '') || 'overview',
  theme: localStorage.getItem('tht-infra-theme') || 'dark',

  // Data slices
  overview: null,
  clients: null,
  clientAccounts: {},
  clientTrends: {},
  zapmail: null,
  domains: null,
  pipelines: null,
  setupPipelines: null,
  acquisition: null,
  acquisitionCampaigns: null,
  genericGroups: null,
  rotationStatus: null,
  wallet: null,
  subscriptions: null,
  placementTests: null,
  domainInventory: null,
  unassigned: null,

  // Loading/error per slice
  loading: {},
  errors: {},

  // Auth
  user: null,
  authReady: false,
};

const listeners = new Map();

function subscribe(key, callback) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(callback);
  return () => listeners.get(key).delete(callback);
}

function notify(key) {
  const cbs = listeners.get(key);
  if (cbs) cbs.forEach(cb => cb(state[key]));
  const wildcards = listeners.get('*');
  if (wildcards) wildcards.forEach(cb => cb(key, state[key]));
}

const store = {
  get(key) {
    return state[key];
  },

  set(key, value) {
    state[key] = value;
    notify(key);
  },

  setLoading(key, isLoading) {
    state.loading[key] = isLoading;
    notify('loading');
  },

  setError(key, error) {
    if (error) {
      state.errors[key] = error;
    } else {
      delete state.errors[key];
    }
    notify('errors');
  },

  setData(key, data, meta) {
    state[key] = data;
    state.loading[key] = false;
    delete state.errors[key];
    if (meta) {
      state[`_meta_${key}`] = meta;
    }
    notify(key);
    notify('loading');
  },

  getMeta(key) {
    return state[`_meta_${key}`] || null;
  },

  setTheme(theme) {
    state.theme = theme;
    localStorage.setItem('tht-infra-theme', theme);
    document.documentElement.setAttribute('data-theme', theme);
    notify('theme');
  },

  setView(view) {
    state.currentView = view;
    location.hash = view;
    notify('currentView');
  },

  subscribe,
};

export { state, store };
