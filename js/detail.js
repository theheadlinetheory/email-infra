// ── Client Detail Panel + Trends ──

function trendIndicator(trend) {
    var icon = trend === 'up' ? '▲' : trend === 'down' ? '▼' : '▶';
    var color = trend === 'up' ? '#22c55e' : trend === 'down' ? '#ef4444' : '#9ca3af';
    return {icon: icon, color: color};
}

function renderNoChartData(canvas, message) {
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#666';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(message, canvas.width / 2, 100);
}

async function openDetail(clientId, clientName) {
    document.getElementById('detail-overlay').style.display = 'block';
    document.getElementById('detail-panel').style.display = 'block';
    document.getElementById('detail-title').textContent = clientName;
    document.getElementById('detail-content').innerHTML = '<div class="loading"><span class="spinner"></span> Loading accounts...</div>';
    currentTrendClientId = clientId;
    currentDetailClientName = clientName;
    var isArchived = (window._archivedClients || []).includes(clientName);
    var archBtn = document.getElementById('archive-btn');
    archBtn.textContent = isArchived ? 'Unarchive' : 'Archive';
    archBtn.style.color = isArchived ? '#22c55e' : 'var(--text-muted)';

    try {
        var [accountsResp, trendsResp] = await Promise.all([
            fetch('/api/client/' + clientId + '/accounts'),
            fetch('/api/client/' + clientId + '/trends?days=30'),
        ]);
        var data = await accountsResp.json();
        var trends = await trendsResp.json();
        renderDetailTable(data, trends);
    } catch (err) {
        document.getElementById('detail-content').innerHTML = 'Error: ' + err.message;
    }
}

function renderDetailTable(data, trends) {
    var accounts = data.accounts || [];

    // Replacement recommendation (1-for-1 disabled — A/B group rotation pending)
    var recHtml = '';
    if (data.flagged_domains && data.flagged_domains.length > 0) {
        recHtml = `<div style="background:var(--red-bg);border:1px solid #3d1519;border-radius:8px;padding:14px 18px;margin-bottom:16px;">
            <div style="font-size:14px;color:var(--red);font-weight:600;margin-bottom:6px;">Infrastructure Replacement Needed</div>
            <div style="font-size:13px;color:#f8a0a0;">${data.flagged_inbox_count} inbox(es) across ${data.flagged_domains.length} domain(s) are unhealthy.</div>
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">Flagged domains: ${data.flagged_domains.join(', ')}</div>
            <div style="font-size:12px;color:#fbbf24;padding:6px 10px;background:rgba(251,191,36,0.1);border-radius:4px;">Replacements will go to the B group once set up. 1-for-1 replacement is disabled.</div>
        </div>`;
    }

    // Reply rate trend chart
    var chartHtml = '';
    if (trends && !trends.error) {
        var s = trends.summary || {};
        var ti = trendIndicator(s.trend);
        var trendIcon = ti.icon;
        var trendColor = ti.color;
        chartHtml = `<div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <div style="font-size:14px;font-weight:600;">Campaign Performance <span style="font-size:11px;color:#666;font-weight:400;">(7-day rolling avg)</span></div>
                <div style="display:flex;gap:4px;" id="trend-zoom-btns">
                    <button onclick="loadTrends(7)" class="trend-zoom" data-days="7" style="background:var(--bg-surface);color:var(--text-muted);border:1px solid var(--border);padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;">7D</button>
                    <button onclick="loadTrends(14)" class="trend-zoom" data-days="14" style="background:var(--bg-surface);color:var(--text-muted);border:1px solid var(--border);padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;">14D</button>
                    <button onclick="loadTrends(30)" class="trend-zoom" data-days="30" style="background:var(--bg-surface);color:var(--accent);border:1px solid #4ecdc4;padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;">30D</button>
                    <button onclick="loadTrends(90)" class="trend-zoom" data-days="90" style="background:var(--bg-surface);color:var(--text-muted);border:1px solid var(--border);padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;">90D</button>
                    <button onclick="loadTrends(0)" class="trend-zoom" data-days="0" style="background:var(--bg-surface);color:var(--text-muted);border:1px solid var(--border);padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;">All</button>
                </div>
            </div>
            <div style="height:200px;position:relative;"><canvas id="trend-chart"></canvas></div>
            <div id="trend-summary" style="display:flex;gap:16px;margin-top:12px;font-size:12px;flex-wrap:wrap;">
                <div><span style="color:var(--accent);">&#9679;</span> <span style="color:var(--text-muted);">Reply Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_reply_rate || 0}%</span></div>
                <div><span style="color:var(--red);">&#9679;</span> <span style="color:var(--text-muted);">Bounce Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_bounce_rate || 0}%</span></div>
                <div><span style="color:var(--text-muted);">Last 7d:</span> <span style="color:${trendColor};font-weight:600;">${s.recent_7d_rate || 0}% ${trendIcon}</span></div>
                <div><span style="color:var(--text-muted);">Prior 7d:</span> <span style="color:var(--text-muted);">${s.prior_7d_rate || 0}%</span></div>
                <div><span style="color:var(--text-muted);">Sent:</span> <span style="color:var(--text-primary);">${(s.total_sent || 0).toLocaleString()}</span></div>
                <div><span style="color:var(--text-muted);">Bounced:</span> <span style="color:var(--text-primary);">${(s.total_bounced || 0).toLocaleString()}</span></div>
                <div><span style="color:var(--text-muted);">Replies:</span> <span style="color:var(--text-primary);">${s.total_replied || 0}</span></div>
            </div>
        </div>`;
    }

    // Idle inbox alert
    var idleHtml = '';
    var idleInboxes = accounts.filter(a => a.warmup_status !== 'ACTIVE' && a.campaign_count === 0 && a.warmup_days !== null && a.warmup_days >= 14);
    if (idleInboxes.length > 0) {
        idleHtml = `<div style="background:var(--yellow-bg);border:1px solid #f59e0b33;border-radius:8px;padding:14px 18px;margin-bottom:16px;">
            <div style="font-size:14px;color:var(--yellow);font-weight:600;margin-bottom:6px;">${idleInboxes.length} warmed inbox(es) not in any campaign</div>
            <div style="font-size:13px;color:#ffd9aa;">${idleInboxes.map(a => a.email).join(', ')}</div>
        </div>`;
    }

    var bounceVal = data.avg_bounce_rate !== null && data.avg_bounce_rate !== undefined ? data.avg_bounce_rate.toFixed(1) + '%' : '—';
    var replyVal = data.avg_reply_rate !== null && data.avg_reply_rate !== undefined ? data.avg_reply_rate.toFixed(1) + '%' : '—';
    var bounceColor = data.avg_bounce_rate !== null ? (data.avg_bounce_rate > 3 ? '#ef4444' : data.avg_bounce_rate > 1 ? '#f59e0b' : '#22c55e') : '#9ca3af';
    var replyColor = data.avg_reply_rate !== null ? (data.avg_reply_rate >= 5 ? '#22c55e' : data.avg_reply_rate >= 2 ? '#f59e0b' : '#ef4444') : '#9ca3af';

    var statsHtml = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:16px;">
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Bounce Rate</div><div style="font-size:20px;font-weight:700;color:${bounceColor};">${bounceVal}</div></div>
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Reply Rate</div><div style="font-size:20px;font-weight:700;color:${replyColor};">${replyVal}</div></div>
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Capacity</div><div style="font-size:20px;font-weight:700;color:var(--accent);">${(data.daily_capacity || 0).toLocaleString()}/day</div></div>
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Sent (7d)</div><div style="font-size:20px;font-weight:700;color:var(--text-primary);">${(data.total_sent || 0).toLocaleString()}</div></div>
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">In Campaign</div><div style="font-size:20px;font-weight:700;color:var(--text-primary);">${data.in_campaign_count || 0}/${accounts.length}</div></div>
        <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:8px;padding:12px 14px;"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">SMTP OK</div><div style="font-size:20px;font-weight:700;color:${data.smtp_ok_count === accounts.length ? '#22c55e' : '#ef4444'};">${data.smtp_ok_count || 0}/${accounts.length}</div></div>
    </div>`;

    var html = recHtml + statsHtml + chartHtml + idleHtml;
    html += '<table style="width:100%; border-collapse:collapse; font-size:13px;">';
    html += '<thead><tr><th>Email</th><th>Health</th><th>Warmup</th><th>Rep</th><th>Bounce</th><th>Reply</th><th>Sent</th><th>Campaigns</th><th>SMTP</th></tr></thead><tbody>';

    accounts.forEach(a => {
        var statusColor = a.warmup_status === 'ACTIVE' ? '#22c55e' : (a.blocked_reason ? '#ef4444' : '#f59e0b');
        var br = a.bounce_rate !== null ? parseFloat(a.bounce_rate) : null;
        var rr = a.reply_rate !== null ? parseFloat(a.reply_rate) : null;
        var brColor = br !== null ? (br > 3 ? '#ef4444' : br > 1 ? '#f59e0b' : '#22c55e') : '#9ca3af';
        var rrColor = rr !== null ? (rr > 5 ? '#22c55e' : rr > 2 ? '#f59e0b' : '#ef4444') : '#9ca3af';

        var healthColor = a.health_score >= 85 ? '#22c55e' : a.health_score >= 60 ? '#f59e0b' : '#ef4444';
        var healthBg = a.health_score >= 85 ? '#f0fdf4' : a.health_score >= 60 ? '#fffbeb' : '#fef2f2';
        var rowBg = a.domain_flagged ? 'background:#2a1a1a;' : '';

        // Flag icons
        var flagIcons = (a.health_flags || []).map(f => {
            var icons = {bounce:'B',reply:'R',reputation:'REP',placement:'P',smtp:'SMTP',blocked:'BLK',warmup_off:'WU'};
            return '<span style="background:var(--red-bg);color:var(--red);padding:1px 4px;border-radius:3px;font-size:10px;margin-left:2px;" title="' + f + '">' + (icons[f]||f) + '</span>';
        }).join('');

        // Warmup cell with progress bar for new accounts
        var warmupCell = `<span style="color:${statusColor}">${a.warmup_status}</span>`;
        if (a.warmup_status === 'ACTIVE' && a.warmup_days !== null && a.warmup_days < 14) {
            var pct = Math.min(100, Math.round(a.warmup_days / 14 * 100));
            warmupCell += `<div style="margin-top:4px;background:var(--bg-input);border-radius:3px;height:4px;width:80px;"><div style="background:var(--purple);height:100%;width:${pct}%;border-radius:3px;"></div></div><div style="font-size:10px;color:var(--text-muted);margin-top:2px;">${a.warmup_days}d / 14d</div>`;
        }
        if (a.blocked_reason) {
            warmupCell += `<br><small style="color:var(--red)">${a.blocked_reason}</small>`;
        }

        html += `<tr style="${rowBg}">
            <td>${a.email}</td>
            <td><span style="background:${healthBg};color:${healthColor};padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;">${a.health_score}</span>${flagIcons}</td>
            <td>${warmupCell}</td>
            <td>${a.warmup_reputation}</td>
            <td style="color:${brColor}">${br !== null ? br.toFixed(1) + '%' : '—'}</td>
            <td style="color:${rrColor}">${rr !== null ? rr.toFixed(1) + '%' : '—'}</td>
            <td>${a.health_sent || a.warmup_sent}</td>
            <td>${a.campaign_count}</td>
            <td style="color:${a.smtp_ok ? '#22c55e' : '#ef4444'}">${a.smtp_ok ? 'OK' : 'FAIL'}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    // Delete + Transition buttons
    html += `<div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);display:flex;gap:12px;justify-content:flex-end;">
        <button onclick="openTransitionModal(${data.client_id},'${data.client_name.replace(/'/g, "\\'")}')" style="background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);padding:8px 18px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;">Transition to Another Client</button>
        <button onclick="openDeleteModal(${data.client_id},'${data.client_name.replace(/'/g, "\\'")}')" style="background:var(--red-bg);color:var(--red);border:1px solid #3d1519;padding:8px 18px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;">Delete All Infrastructure</button>
    </div>`;

    document.getElementById('detail-content').innerHTML = html;

    // Render chart after DOM update
    if (trends && !trends.error && trends.data && trends.data.some(d => d.reply_rate !== null)) {
        renderTrendChart(trends);
    } else {
        renderNoChartData(document.getElementById('trend-chart'), 'No campaign data yet');
    }
}

function renderTrendChart(trends) {
    var canvas = document.getElementById('trend-chart');
    if (!canvas) return;

    if (trendChart) { trendChart.destroy(); trendChart = null; }

    var points = trends.data.filter(d => d.reply_rate !== null);
    if (points.length === 0) {
        renderNoChartData(canvas, 'No sending data in this period');
        return;
    }

    var s = trends.summary || {};
    var ti = trendIndicator(s.trend);
    var trendIcon = ti.icon;
    var trendColor = ti.color;
    var summaryEl = document.getElementById('trend-summary');
    if (summaryEl) {
        summaryEl.innerHTML = `
            <div><span style="color:var(--accent);">●</span> <span style="color:var(--text-muted);">Reply Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_reply_rate || 0}%</span></div>
            <div><span style="color:var(--red);">●</span> <span style="color:var(--text-muted);">Bounce Rate:</span> <span style="color:var(--text-primary);font-weight:600;">${s.avg_bounce_rate || 0}%</span></div>
            <div><span style="color:var(--text-muted);">Last 7d:</span> <span style="color:${trendColor};font-weight:600;">${s.recent_7d_rate || 0}% ${trendIcon}</span></div>
            <div><span style="color:var(--text-muted);">Prior 7d:</span> <span style="color:var(--text-muted);">${s.prior_7d_rate || 0}%</span></div>
            <div><span style="color:var(--text-muted);">Sent:</span> <span style="color:var(--text-primary);">${(s.total_sent || 0).toLocaleString()}</span></div>
            <div><span style="color:var(--text-muted);">Bounced:</span> <span style="color:var(--text-primary);">${(s.total_bounced || 0).toLocaleString()}</span></div>
            <div><span style="color:var(--text-muted);">Replies:</span> <span style="color:var(--text-primary);">${s.total_replied || 0}</span></div>
        `;
    }

    var ctx = canvas.getContext('2d');
    trendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: points.map(d => d.date),
            datasets: [{
                label: 'Reply Rate',
                data: points.map(d => d.reply_rate),
                borderColor: '#22c55e',
                backgroundColor: 'rgba(64, 224, 208, 0.08)',
                borderWidth: 2,
                pointRadius: 3,
                pointBackgroundColor: '#22c55e',
                pointHoverRadius: 6,
                fill: true,
                tension: 0.3,
            }, {
                label: 'Bounce Rate',
                data: points.map(d => d.bounce_rate),
                borderColor: '#ef4444',
                backgroundColor: 'rgba(239, 68, 68, 0.05)',
                borderWidth: 2,
                pointRadius: 2,
                pointBackgroundColor: '#ef4444',
                pointHoverRadius: 5,
                fill: true,
                tension: 0.3,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    align: 'end',
                    labels: { color: '#9ca3af', boxWidth: 12, padding: 12, font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: '#16213e',
                    borderColor: '#e5e7eb',
                    borderWidth: 1,
                    titleColor: '#eee',
                    bodyColor: '#eee',
                    callbacks: {
                        label: function(context) {
                            var pt = points[context.dataIndex];
                            if (context.datasetIndex === 0) {
                                return 'Reply Rate: ' + pt.reply_rate + '% (' + pt.replied + '/' + pt.sent.toLocaleString() + ' sent)';
                            }
                            return 'Bounce Rate: ' + pt.bounce_rate + '% (' + pt.bounced + '/' + pt.sent.toLocaleString() + ' sent)';
                        }
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#666', font: { size: 10 }, maxTicksLimit: 12 },
                    grid: { color: '#1a2744' },
                },
                y: {
                    beginAtZero: true,
                    ticks: { color: '#666', font: { size: 10 }, callback: v => v + '%' },
                    grid: { color: '#1a2744' },
                }
            }
        }
    });
}

async function loadTrends(days) {
    if (!currentTrendClientId) return;
    // Update zoom button styles
    document.querySelectorAll('.trend-zoom').forEach(btn => {
        var isActive = parseInt(btn.dataset.days) === days;
        btn.style.color = isActive ? '#22c55e' : '#9ca3af';
        btn.style.borderColor = isActive ? '#22c55e' : '#e5e7eb';
    });
    try {
        var resp = await fetch('/api/client/' + currentTrendClientId + '/trends?days=' + days);
        var trends = await resp.json();
        if (trends && !trends.error) {
            renderTrendChart(trends);
        }
    } catch(e) { console.error('Trend load error:', e); }
}

function closeDetail() {
    document.getElementById('detail-overlay').style.display = 'none';
    document.getElementById('detail-panel').style.display = 'none';
    if (trendChart) { trendChart.destroy(); trendChart = null; }
    currentTrendClientId = null;
}

async function toggleArchiveClient() {
    if (!currentDetailClientName) return;
    var isArchived = (window._archivedClients || []).includes(currentDetailClientName);
    var action = isArchived ? 'unarchive' : 'archive';
    if (!confirm(`${action.charAt(0).toUpperCase() + action.slice(1)} "${currentDetailClientName}"?`)) return;
    try {
        var result = await apiPost('/api/client/archive', {client_name: currentDetailClientName, archived: !isArchived});
        if (result.ok) {
            window._archivedClients = result.archived_clients || [];
            var archBtn = document.getElementById('archive-btn');
            archBtn.textContent = !isArchived ? 'Unarchive' : 'Archive';
            archBtn.style.color = !isArchived ? '#22c55e' : 'var(--text-muted)';
            closeDetail();
            loadOverview();
        }
    } catch(e) { console.error('Archive error:', e); }
}
