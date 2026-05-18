// ── Shared Fetch Helpers ──

async function api(path, options) {
    var resp = await fetch(path, options);
    return resp.json();
}

async function apiPost(path, body) {
    return api(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
}
