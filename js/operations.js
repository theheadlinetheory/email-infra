// ── SSE-Based Modal Operations (Assign, Delete, Transition) ──

// --- Shared SSE helper ---

function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
}

function handleSSEEvent(evt, prefix, totalSteps, onComplete) {
    if (evt.status === 'complete') {
        if (onComplete) onComplete();
        return;
    }
    if (evt.step === 0 && evt.status === 'error') {
        document.getElementById(prefix + '-result').style.display = 'block';
        document.getElementById(prefix + '-result').innerHTML =
            '<div style="color:var(--red);">' + evt.message + '</div>' +
            '<button onclick="close' + capitalize(prefix) + 'Modal()" style="background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:8px 18px;border-radius:6px;cursor:pointer;margin-top:8px;">Close</button>';
        return;
    }

    var icon = document.getElementById(prefix + '-icon-' + evt.step);
    var label = document.getElementById(prefix + '-label-' + evt.step);
    if (!icon || !label) return;

    if (evt.status === 'running') {
        icon.innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px;display:inline-block;"></span>';
        label.style.color = '#eee';
    } else if (evt.status === 'done') {
        icon.innerHTML = '✓';
        icon.style.color = '#22c55e';
        label.style.color = '#22c55e';
        if (evt.message) label.textContent += ' — ' + evt.message;
    } else if (evt.status === 'error') {
        icon.innerHTML = '✗';
        icon.style.color = '#ef4444';
        label.style.color = '#ef4444';
        for (var i = evt.step + 1; i <= totalSteps; i++) {
            var si = document.getElementById(prefix + '-icon-' + i);
            var sl = document.getElementById(prefix + '-label-' + i);
            if (si) { si.innerHTML = '—'; si.style.color = '#444'; }
            if (sl) sl.style.color = '#444';
        }
        document.getElementById(prefix + '-result').style.display = 'block';
        document.getElementById(prefix + '-result').innerHTML =
            '<div style="color:var(--red);font-size:13px;margin-bottom:8px;">' + evt.message + '</div>' +
            '<button onclick="close' + capitalize(prefix) + 'Modal()" style="background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:8px 18px;border-radius:6px;cursor:pointer;">Close</button>';
    }
}

function runSSEOperation(url, body, prefix, steps, totalSteps, onComplete, onError) {
    // Render step list
    var stepsHtml = '';
    steps.forEach(function(s) {
        stepsHtml += '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;font-size:14px;">' +
            '<span id="' + prefix + '-icon-' + s.id + '" style="width:22px;text-align:center;color:#555;">○</span>' +
            '<span id="' + prefix + '-label-' + s.id + '" style="color:var(--text-muted);">' + s.label + '</span>' +
            '</div>';
    });
    document.getElementById(prefix + '-steps').innerHTML = stepsHtml;
    document.getElementById(prefix + '-result').style.display = 'none';

    fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    }).then(function(response) {
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        function read() {
            reader.read().then(function(result) {
                if (result.done) return;
                buffer += decoder.decode(result.value, {stream: true});
                var lines = buffer.split('\n');
                buffer = lines.pop();
                lines.forEach(function(line) {
                    if (line.startsWith('data: ')) {
                        try {
                            var evt = JSON.parse(line.slice(6));
                            handleSSEEvent(evt, prefix, totalSteps, onComplete);
                        } catch(e) {}
                    }
                });
                read();
            });
        }
        read();
    }).catch(function(err) {
        if (onError) onError();
        document.getElementById(prefix + '-result').style.display = 'block';
        document.getElementById(prefix + '-result').innerHTML =
            '<div style="color:var(--red);">Connection error: ' + err.message + '</div>' +
            '<button onclick="close' + capitalize(prefix) + 'Modal()" style="background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:8px 18px;border-radius:6px;cursor:pointer;margin-top:8px;">Close</button>';
    });
}

// --- Shared modal helpers ---

function populateClientDropdown(selectId, excludeClient) {
    var select = document.getElementById(selectId);
    select.innerHTML = '<option value="">Loading...</option>';
    fetch('/api/clients/list').then(function(resp) {
        return resp.json();
    }).then(function(data) {
        var clients = (data.clients || []);
        if (excludeClient) clients = clients.filter(function(c) { return c !== excludeClient; });
        var opts = '<option value="">Select a client...</option>';
        clients.forEach(function(c) { opts += '<option value="' + c + '">' + c + '</option>'; });
        opts += '<option value="__new__">+ Add New Client</option>';
        select.innerHTML = opts;
    }).catch(function() {
        select.innerHTML = '<option value="">Error loading clients</option>';
    });
}

function setButtonReady(btnId, ready) {
    document.getElementById(btnId).disabled = !ready;
    document.getElementById(btnId).style.opacity = ready ? '1' : '0.5';
}

// --- Assign to Client ---

function openAssignModal(pipelineId, groupName) {
    document.getElementById('ac-pipeline-id').value = pipelineId;
    document.getElementById('ac-group-label').textContent = 'Reassigning: ' + groupName;
    document.getElementById('assign-form').style.display = 'block';
    document.getElementById('assign-progress').style.display = 'none';
    document.getElementById('ac-new-client-row').style.display = 'none';
    document.getElementById('ac-forwarding').value = '';
    setButtonReady('ac-assign-btn', false);
    document.getElementById('assign-overlay').style.display = 'block';
    document.getElementById('assign-modal').style.display = 'block';
    populateClientDropdown('ac-client-select');
}

function closeAssignModal() {
    if (assignmentInProgress) return;
    document.getElementById('assign-overlay').style.display = 'none';
    document.getElementById('assign-modal').style.display = 'none';
}

function onClientSelectChange() {
    var val = document.getElementById('ac-client-select').value;
    document.getElementById('ac-new-client-row').style.display = val === '__new__' ? 'block' : 'none';
    if (val !== '__new__') document.getElementById('ac-new-client-name').value = '';
    checkAssignReady();
}

function checkAssignReady() {
    var selectVal = document.getElementById('ac-client-select').value;
    var newName = document.getElementById('ac-new-client-name').value.trim();
    var fwd = document.getElementById('ac-forwarding').value.trim();
    var hasClient = selectVal === '__new__' ? newName.length > 0 : selectVal.length > 0;
    setButtonReady('ac-assign-btn', hasClient && fwd.length > 0);
}

function startAssignment() {
    var pipelineId = document.getElementById('ac-pipeline-id').value;
    var selectVal = document.getElementById('ac-client-select').value;
    var isNew = selectVal === '__new__';
    var clientName = isNew ? document.getElementById('ac-new-client-name').value.trim() : selectVal;
    var fwd = document.getElementById('ac-forwarding').value.trim();
    var abGroup = document.getElementById('ac-ab-group').value;

    assignmentInProgress = true;
    document.getElementById('assign-form').style.display = 'none';
    document.getElementById('assign-progress').style.display = 'block';
    document.getElementById('ac-progress-client').textContent = clientName;

    runSSEOperation(
        '/api/pipeline/assign-client',
        {pipeline_id: pipelineId, client_name: clientName, forwarding_domain: fwd, is_new_client: isNew, ab_group: abGroup},
        'ac',
        ASSIGN_STEPS,
        7,
        function() {
            assignmentInProgress = false;
            document.getElementById('ac-result').style.display = 'block';
            document.getElementById('ac-result').innerHTML =
                '<div style="color:var(--accent);font-weight:600;margin-bottom:8px;">Assignment complete!</div>' +
                '<button onclick="closeAssignModal();loadOverview();" style="background:var(--accent);color:var(--bg-root);border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>';
        },
        function() {
            assignmentInProgress = false;
        }
    );
}

// --- Delete Infrastructure ---

var _deletionInProgress = {};

function openDeleteModal(clientId, clientName) {
    if (_deletionInProgress[clientId]) {
        document.getElementById('delete-confirm').style.display = 'none';
        document.getElementById('delete-progress').style.display = 'block';
        document.getElementById('delete-overlay').style.display = 'block';
        document.getElementById('delete-modal').style.display = 'block';
        return;
    }
    document.getElementById('del-client-id').value = clientId;
    document.getElementById('del-client-name').value = clientName;
    document.getElementById('del-client-label').textContent = clientName;
    document.getElementById('del-confirm-name').textContent = clientName;
    document.getElementById('del-confirm-input').value = '';
    document.getElementById('del-step1').style.display = 'block';
    document.getElementById('del-step2').style.display = 'none';
    document.getElementById('delete-confirm').style.display = 'block';
    document.getElementById('delete-progress').style.display = 'none';
    setButtonReady('del-final-btn', false);
    document.getElementById('delete-overlay').style.display = 'block';
    document.getElementById('delete-modal').style.display = 'block';
}

function closeDeleteModal() {
    document.getElementById('delete-overlay').style.display = 'none';
    document.getElementById('delete-modal').style.display = 'none';
}

function showDeleteStep2() {
    document.getElementById('del-step1').style.display = 'none';
    document.getElementById('del-step2').style.display = 'block';
    document.getElementById('del-confirm-input').focus();
}

function checkDeleteConfirm() {
    var input = document.getElementById('del-confirm-input').value.trim();
    var expected = document.getElementById('del-client-name').value.trim();
    setButtonReady('del-final-btn', input.toLowerCase() === expected.toLowerCase());
}

function startDeletion() {
    var clientId = document.getElementById('del-client-id').value;
    var clientName = document.getElementById('del-client-name').value;
    _deletionInProgress[clientId] = true;

    document.getElementById('delete-confirm').style.display = 'none';
    document.getElementById('delete-progress').style.display = 'block';
    document.getElementById('del-progress-client').textContent = clientName;

    runSSEOperation(
        '/api/client/delete-infra',
        {client_id: clientId, client_name: clientName},
        'del',
        DELETE_STEPS,
        5,
        function() {
            document.getElementById('del-result').style.display = 'block';
            document.getElementById('del-result').innerHTML =
                '<div style="color:var(--accent);font-weight:600;margin-bottom:8px;">Infrastructure deleted successfully.</div>' +
                '<button onclick="closeDeleteModal();closeDetail();loadOverview();" style="background:var(--accent);color:var(--bg-root);border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>';
        },
        null
    );
}

// --- Transition Infrastructure ---

function openTransitionModal(clientId, clientName) {
    document.getElementById('tr-client-id').value = clientId;
    document.getElementById('tr-client-name').value = clientName;
    document.getElementById('tr-source-label').textContent = 'Transitioning from: ' + clientName;
    document.getElementById('tr-new-client-row').style.display = 'none';
    document.getElementById('tr-forwarding').value = '';
    setButtonReady('tr-start-btn', false);
    document.getElementById('transition-form').style.display = 'block';
    document.getElementById('transition-progress').style.display = 'none';
    document.getElementById('transition-overlay').style.display = 'block';
    document.getElementById('transition-modal').style.display = 'block';
    populateClientDropdown('tr-client-select', clientName);
}

function closeTransitionModal() {
    document.getElementById('transition-overlay').style.display = 'none';
    document.getElementById('transition-modal').style.display = 'none';
}

function onTransitionClientChange() {
    var val = document.getElementById('tr-client-select').value;
    document.getElementById('tr-new-client-row').style.display = val === '__new__' ? 'block' : 'none';
    if (val !== '__new__') document.getElementById('tr-new-client-name').value = '';
    checkTransitionReady();
}

function checkTransitionReady() {
    var selectVal = document.getElementById('tr-client-select').value;
    var newName = document.getElementById('tr-new-client-name').value.trim();
    var fwd = document.getElementById('tr-forwarding').value.trim();
    var hasClient = selectVal === '__new__' ? newName.length > 0 : selectVal.length > 0;
    setButtonReady('tr-start-btn', hasClient && fwd.length > 0);
}

function startTransition() {
    var clientId = document.getElementById('tr-client-id').value;
    var clientName = document.getElementById('tr-client-name').value;
    var selectVal = document.getElementById('tr-client-select').value;
    var isNew = selectVal === '__new__';
    var newClientName = isNew ? document.getElementById('tr-new-client-name').value.trim() : selectVal;
    var fwd = document.getElementById('tr-forwarding').value.trim();

    document.getElementById('transition-form').style.display = 'none';
    document.getElementById('transition-progress').style.display = 'block';
    document.getElementById('tr-progress-client').textContent = newClientName;

    runSSEOperation(
        '/api/client/transition',
        {client_id: clientId, client_name: clientName, new_client_name: newClientName, forwarding_domain: fwd, is_new_client: isNew},
        'tr',
        TRANSITION_STEPS,
        6,
        function() {
            document.getElementById('tr-result').style.display = 'block';
            document.getElementById('tr-result').innerHTML =
                '<div style="color:var(--accent);font-weight:600;margin-bottom:8px;">Transition complete!</div>' +
                '<button onclick="closeTransitionModal();closeDetail();loadOverview();" style="background:var(--accent);color:var(--bg-root);border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>';
        },
        null
    );
}
