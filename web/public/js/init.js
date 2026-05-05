// ── Initialization (loaded last) ──

initTheme();

document.getElementById('assign-client-select').addEventListener('change', updateAssignBtn);

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

initSupabaseRealtime();
loadOverview();
loadWallet();

// Auto-refresh every 5 minutes
setInterval(loadOverview, 5 * 60 * 1000);
setInterval(loadWallet, 5 * 60 * 1000);
