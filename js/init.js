// ── Initialization (loaded last) ──

initTheme();

// Auto-set auth cookie from ?pw= query param
(function() {
    var pw = new URLSearchParams(window.location.search).get('pw');
    if (pw) {
        document.cookie = 'dashboard_pw=' + encodeURIComponent(pw) + ';path=/;max-age=31536000;SameSite=Lax';
    }
})();

async function checkAuthAndInit() {
    var resp = await fetch('/api/auth-check');
    if (resp.ok) {
        startApp();
        return;
    }
    document.getElementById('loading').innerHTML =
        '<div style="text-align:center;margin-top:40px;">' +
        '<h3 style="color:var(--text);margin-bottom:16px;">Dashboard Login</h3>' +
        '<input id="pw-input" type="password" placeholder="Password" ' +
        'style="padding:10px 16px;border-radius:6px;border:1px solid var(--border);background:var(--bg-raised);color:var(--text);font-size:14px;width:240px;">' +
        '<button id="pw-btn" style="margin-left:8px;padding:10px 20px;border-radius:6px;border:none;background:var(--accent);color:#0d0d14;font-weight:600;cursor:pointer;">Login</button>' +
        '<div id="pw-error" style="color:var(--red);margin-top:8px;font-size:12px;"></div>' +
        '</div>';
    var input = document.getElementById('pw-input');
    var btn = document.getElementById('pw-btn');
    function tryLogin() {
        var val = input.value.trim();
        if (!val) return;
        document.cookie = 'dashboard_pw=' + encodeURIComponent(val) + ';path=/;max-age=31536000;SameSite=Lax';
        fetch('/api/auth-check').then(function(r) {
            if (r.ok) {
                document.getElementById('loading').innerHTML = '<span class="spinner"></span> Loading infrastructure data...';
                startApp();
            } else {
                document.cookie = 'dashboard_pw=;path=/;max-age=0';
                document.getElementById('pw-error').textContent = 'Invalid password';
            }
        });
    }
    btn.addEventListener('click', tryLogin);
    input.addEventListener('keydown', function(e) { if (e.key === 'Enter') tryLogin(); });
    input.focus();
}

function startApp() {
    var el = document.getElementById('assign-client-select');
    if (el) el.addEventListener('change', updateAssignBtn);
    initSupabaseRealtime();
    loadOverview();
    loadWallet();
    setInterval(loadOverview, 5 * 60 * 1000);
    setInterval(loadWallet, 5 * 60 * 1000);
}

async function initSupabaseRealtime() {
    try {
        var resp = await fetch('/api/supabase-config');
        var cfg = await resp.json();
        if (!cfg.url || !cfg.key) return;

        var script = document.createElement('script');
        script.src = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js';
        script.onload = function() {
            var sb = supabase.createClient(cfg.url, cfg.key);
            realtimeChannel = sb.channel('setup-pipelines-changes')
                .on('postgres_changes', { event: '*', schema: 'public', table: 'setup_pipelines' }, function(payload) {
                    handlePipelineRealtimeUpdate(payload);
                })
                .subscribe(function(status) {
                    if (status === 'SUBSCRIBED') {
                        console.log('Realtime: subscribed to setup_pipelines');
                        if (setupPipelinePollInterval) {
                            clearInterval(setupPipelinePollInterval);
                            setupPipelinePollInterval = null;
                        }
                    }
                });
        };
        document.head.appendChild(script);
    } catch (e) {
        console.warn('Realtime init failed, falling back to polling:', e);
    }
}

function handlePipelineRealtimeUpdate(payload) {
    var row = payload.new;
    if (!row) return;

    var steps = typeof row.steps === 'string' ? JSON.parse(row.steps) : (row.steps || []);
    var currentStep = row.current_step || 0;

    if (payload.eventType === 'UPDATE') {
        var completedStep = steps.find(function(s) { return s.status === 'done' && s.step === currentStep - 1; });
        if (completedStep) {
            showToast(row.name + ': ' + completedStep.name + ' complete', 'success');
        }
        var failedStep = steps.find(function(s) { return s.status === 'failed'; });
        if (failedStep) {
            showToast(row.name + ': ' + failedStep.name + ' failed — ' + (failedStep.error || 'unknown error'), 'error', 8000);
        }
        if (row.status === 'complete') {
            showToast(row.name + ' pipeline complete!', 'success', 8000);
        }
    }

    loadSetupPipelines();
}

checkAuthAndInit();
