/**
 * Step-by-step SSE progress display for long-running operations.
 * Shows steps with status indicators as events arrive.
 */

export function sseProgress({ steps = [], title = '' }) {
  const container = document.createElement('div');
  container.className = 'sse-progress';

  if (title) {
    const h3 = document.createElement('h3');
    h3.textContent = title;
    h3.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:16px;';
    container.appendChild(h3);
  }

  const stepList = document.createElement('div');
  stepList.className = 'pill-stepper';
  container.appendChild(stepList);

  const log = document.createElement('div');
  log.style.cssText = 'margin-top:12px;font-size:13px;color:var(--text-secondary);font-family:var(--font-mono);max-height:200px;overflow-y:auto;';
  container.appendChild(log);

  function update(event) {
    const { step, total, status, message } = event;

    // Rebuild step pills
    stepList.innerHTML = '';
    for (let i = 1; i <= (total || steps.length); i++) {
      if (i > 1) {
        const conn = document.createElement('div');
        conn.className = `pill-connector ${i <= step ? 'done' : 'pending'}`;
        stepList.appendChild(conn);
      }
      const pill = document.createElement('div');
      const stepName = steps[i - 1] || `Step ${i}`;
      if (i < step) {
        pill.className = 'pill-step completed';
        pill.innerHTML = `<span class="pill-icon">✓</span>${esc(stepName)}`;
      } else if (i === step) {
        pill.className = `pill-step ${status === 'error' ? 'failed' : 'running'}`;
        pill.innerHTML = `<span class="pill-icon">${status === 'error' ? '✕' : '◉'}</span>${esc(stepName)}`;
      } else {
        pill.className = 'pill-step pending';
        pill.textContent = stepName;
      }
      stepList.appendChild(pill);
    }

    // Add log entry
    if (message) {
      const entry = document.createElement('div');
      entry.style.padding = '2px 0';
      entry.style.color = status === 'error' ? 'var(--red)' : 'var(--text-secondary)';
      entry.textContent = message;
      log.appendChild(entry);
      log.scrollTop = log.scrollHeight;
    }
  }

  return { element: container, update };
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
