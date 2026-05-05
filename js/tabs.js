// ── Navigation ──

function switchMode(mode) {
    currentMode = mode;
    localStorage.setItem('dashboardMode', mode);
    applyModeVisibility(mode);
    if (overviewData) {
        renderOverview();
    }
}

function switchTab(tab) {
    document.querySelectorAll('.nav-tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function(t) { t.classList.remove('active'); });
    document.querySelector('.nav-tab[onclick="switchTab(\'' + tab + '\')"]').classList.add('active');
    document.getElementById('tab-' + tab).classList.add('active');

    if (tab === 'zapmail' && !zmData) loadZapmail();
    if (tab === 'domains' && !domData) loadDomains();
    if (tab === 'pipelines') loadPipelines();
    if (tab === 'sync' && !syncData) loadSync();
}
