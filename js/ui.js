// ── Theme + Toast + Shared Utilities ──

function initTheme() {
    var saved = localStorage.getItem('tht-theme');
    if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.classList.add('dark');
    }
    updateThemeIcon();
}

function toggleTheme() {
    document.documentElement.classList.toggle('dark');
    localStorage.setItem('tht-theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light');
    updateThemeIcon();
}

function updateThemeIcon() {
    var btn = document.getElementById('theme-toggle-btn');
    if (btn) btn.innerHTML = document.documentElement.classList.contains('dark') ? '&#9788;' : '&#9790;';
}

function ensureToastContainer() {
    var c = document.getElementById('toast-container');
    if (!c) {
        c = document.createElement('div');
        c.id = 'toast-container';
        c.className = 'toast-container';
        document.body.appendChild(c);
    }
    return c;
}

function showToast(message, type, duration) {
    type = type || 'info';
    duration = duration || 5000;
    var container = ensureToastContainer();
    var icons = { success: '✅', error: '❌', info: 'ℹ️' };
    var toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.innerHTML = '<span class="toast-icon">' + (icons[type] || icons.info) + '</span>' +
        '<span class="toast-msg">' + message + '</span>' +
        '<button class="toast-close" onclick="this.parentElement.remove()">&times;</button>';
    container.appendChild(toast);
    setTimeout(function() {
        toast.style.animation = 'toast-out .3s ease-in forwards';
        setTimeout(function() { toast.remove(); }, 300);
    }, duration);
}
