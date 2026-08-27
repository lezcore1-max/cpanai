const API = ""; // same-origin; FastAPI serves this frontend directly

let USE_CASES = {};
let SAMPLES = {};
let currentUC = null;

const CIRCUMFERENCE = 238.76; // 2 * PI * 38 for SVG ring

async function boot() {
  try {
    const list = await (await fetch(`${API}/api/use-cases`)).json();
    USE_CASES = Object.fromEntries(list.map(uc => [uc.key, uc]));
    currentUC = list[0].key;
    setStatus(true);
  } catch (e) {
    setStatus(false);
    return;
  }
  renderUseCaseTabs();
  await loadSamples();
  renderPolicyNote();
  await refreshLog();

  document.getElementById('runBtn').onclick = runInspection;
  document.getElementById('clearBtn').onclick = clearLog;
}

function setStatus(ok) {
  const el = document.getElementById('apiStatus');
  const txt = document.getElementById('statusText');
  el.className = 'status-pill' + (ok ? '' : ' error');
  txt.textContent = ok ? 'SYSTEM ONLINE · API READY' : 'API UNREACHABLE — SERVER DOWN';
}

function renderUseCaseTabs() {
  const row = document.getElementById('ucRow');
  row.innerHTML = '';
  Object.values(USE_CASES).forEach(uc => {
    const btn = document.createElement('div');
    btn.className = 'uc-btn' + (uc.key === currentUC ? ' active' : '');
    btn.onclick = async () => {
      currentUC = uc.key;
      renderUseCaseTabs();
      await loadSamples();
      renderPolicyNote();
      resetGauges();
    };
    const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
    const tags = [
      `${uc.latency_budget_ms}ms SLA`,
      uc.pipeline_position.split(' —')[0],
      isPostHoc ? '⚙ ASYNC MODE' : '⚡ BLOCKING GATE',
    ];
    btn.innerHTML = `
      <div class="uc-name">
        ${uc.label}
        <span style="font-family:var(--font-mono);font-size:10px;color:var(--accent-cyan);">${uc.key.toUpperCase()}</span>
      </div>
      <div style="font-size:11.5px;color:var(--text-muted);margin-top:4px;">${uc.description}</div>
      <div class="uc-meta">${tags.map(t => `<span class="uc-tag">${t}</span>`).join('')}</div>
    `;
    row.appendChild(btn);
  });
}

async function loadSamples() {
  if (!SAMPLES[currentUC]) {
    SAMPLES[currentUC] = await (await fetch(`${API}/api/samples/${currentUC}`)).json();
  }
  const row = document.getElementById('sampleRow');
  row.innerHTML = '';
  SAMPLES[currentUC].forEach(s => {
    const b = document.createElement('button');
    b.className = 'sample-btn';
    b.textContent = 'Load: ' + s.label;
    b.onclick = () => {
      document.getElementById('question').value = s.question;
      document.getElementById('context').value = s.context;
      document.getElementById('response').value = s.response;
    };
    row.appendChild(b);
  });
}

function renderPolicyNote() {
  const uc = USE_CASES[currentUC];
  const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
  document.getElementById('policyNote').innerHTML = `
    <b style="color:var(--accent-cyan)">POLICY CONFIGURATION — ${uc.label.toUpperCase()}:</b> ${uc.pipeline_position}<br>
    <b>Risk Weights:</b> Responsibility ${Math.round(uc.weights.responsibility * 100)}% · Performance ${Math.round(uc.weights.performance * 100)}% · Cost ${Math.round(uc.weights.cost * 100)}%<br>
    <b>Action Thresholds:</b> BLOCK ≥ ${uc.thresholds.block} · HUMAN ≥ ${uc.thresholds.human} · FIX ≥ ${uc.thresholds.fix}<br>
    <span style="color:${isPostHoc ? 'var(--human)' : 'var(--safe)'};font-weight:500;">
      ${isPostHoc
        ? '⚙ Post-hoc audit mode — inspection executes asynchronously in background tasks.'
        : '⚡ Pre-response gate — synchronous execution holds response until inspection completes.'}
    </span>
  `;
}

/* ── Smooth Color & SVG Gauge Ring Controller ─────────────────────────── */

function getScoreColor(score) {
  const clamped = Math.max(0, Math.min(100, score));
  let hue;
  if (clamped <= 50) {
    hue = 142 - (clamped / 50) * (142 - 38); // 142 (green) -> 38 (amber)
  } else {
    hue = 38 - ((clamped - 50) / 50) * 44;   // 38 (amber) -> -6 / 354 (red)
    if (hue < 0) hue += 360;
  }
  return `hsl(${Math.round(hue)}, 85%, 52%)`;
}

function resetGauges() {
  ['resp', 'perf', 'cost'].forEach(k => {
    const ring = document.getElementById('ring-' + k);
    const val = document.getElementById('val-' + k);
    const sub = document.getElementById('sub-' + k);
    if (ring) {
      ring.style.strokeDashoffset = CIRCUMFERENCE;
      ring.style.stroke = 'var(--safe)';
    }
    if (val) val.textContent = '—';
    if (sub) {
      if (k === 'resp') sub.textContent = 'PII & Bias Regex';
      if (k === 'perf') sub.textContent = 'LLM Groundedness';
      if (k === 'cost') sub.textContent = 'Token Budget';
    }
  });
  const latencyBadge = document.getElementById('latencyBadge');
  if (latencyBadge) latencyBadge.style.display = 'none';
}

function setGaugeScore(k, score, detail) {
  const ring = document.getElementById('ring-' + k);
  const val = document.getElementById('val-' + k);
  const sub = document.getElementById('sub-' + k);
  const clamped = Math.max(0, Math.min(100, Math.round(score)));
  const color = getScoreColor(clamped);

  if (ring) {
    const offset = CIRCUMFERENCE - (clamped / 100) * CIRCUMFERENCE;
    ring.style.stroke = color;
    ring.style.strokeDashoffset = offset;
  }

  if (val) {
    val.textContent = clamped;
    val.style.color = color;
  }

  if (sub && detail) {
    sub.textContent = detail;
  }
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

/* ── Routing: pipeline_position drives sync vs async execution ──────── */

async function runInspection() {
  const uc = USE_CASES[currentUC];
  const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
  if (isPostHoc) {
    await runAsyncInspection();
  } else {
    await runSyncInspection();
  }
}

/* ── Synchronous Inspection (Chatbot / Copilot — Pre-response / Inline) ─ */

async function runSyncInspection() {
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response) {
    alert('Please paste an AI response to inspect first.');
    return;
  }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  runBtn.innerHTML = `
    <svg class="play-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="10" style="animation:spin 1s linear infinite"/>
    </svg>
    <span>Inspecting Response…</span>
  `;

  resetGauges();
  const badge = document.getElementById('decisionBadge');
  badge.className = 'decision-badge IDLE';
  document.getElementById('decisionText').textContent = 'EVALUATING CHECKS…';
  document.getElementById('decisionSub').textContent = 'Running Responsibility, Performance, and Cost checks concurrently...';
  document.getElementById('reasoningBox').innerHTML = '<div class="reasoning-placeholder">Executing inspection pipeline...</div>';
  document.getElementById('fixPanel').innerHTML = '';

  let result;
  try {
    const res = await fetch(`${API}/api/inspect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_case: currentUC, question, context, response })
    });
    if (!res.ok) {
      throw new Error((await res.json()).detail || 'Inspection request failed');
    }
    result = await res.json();
  } catch (e) {
    badge.className = 'decision-badge BLOCK';
    document.getElementById('decisionText').textContent = 'ERROR';
    document.getElementById('decisionSub').textContent = 'Inspection failed: ' + e.message;
    document.getElementById('reasoningBox').innerHTML = `<div class="override-note">❌ Inspection API Exception: ${escapeHtml(e.message)}</div>`;
    runBtn.disabled = false;
    runBtn.innerHTML = `
      <svg class="play-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <polygon points="5 3 19 12 5 21 5 3"/>
      </svg>
      <span>Execute Risk Inspection</span>
    `;
    return;
  }

  // Update SVG Risk Rings
  setGaugeScore('resp', result.responsibility_score, `${result.responsibility_flags.length} flag(s)`);
  setGaugeScore('perf', result.performance_score, `${result.performance_confidence} conf.`);
  setGaugeScore('cost', result.cost_score, `~${result.estimated_tokens} tokens`);

  renderDecision(result);
  await refreshLog();

  runBtn.disabled = false;
  runBtn.innerHTML = `
    <svg class="play-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span>Execute Risk Inspection</span>
  `;
}

/* ── Asynchronous Inspection (Decision — Post-hoc Audit) ─────────────── */

async function runAsyncInspection() {
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response) {
    alert('Please paste an AI response to inspect first.');
    return;
  }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  runBtn.innerHTML = `<span>Queueing Audit Task…</span>`;
  resetGauges();

  const badge = document.getElementById('decisionBadge');
  badge.className = 'decision-badge IDLE';
  document.getElementById('decisionText').textContent = 'DISPATCHING ASYNC AUDIT…';
  document.getElementById('reasoningBox').innerHTML = '';
  document.getElementById('fixPanel').innerHTML = '';

  let queued;
  try {
    const res = await fetch(`${API}/api/inspect-async`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_case: currentUC, question, context, response })
    });
    if (!res.ok) {
      throw new Error((await res.json()).detail || 'Async inspection request failed');
    }
    queued = await res.json();
  } catch (e) {
    badge.className = 'decision-badge BLOCK';
    document.getElementById('decisionText').textContent = 'ERROR';
    document.getElementById('decisionSub').textContent = 'Request failed: ' + e.message;
    runBtn.disabled = false;
    runBtn.innerHTML = `<span>Execute Risk Inspection</span>`;
    return;
  }

  badge.className = 'decision-badge HUMAN';
  document.getElementById('decisionText').textContent = 'POST-HOC AUDIT QUEUED';
  document.getElementById('decisionSub').textContent = `Task ID #${queued.queued_id} dispatched to background queue (SLA Budget: ${queued.latency_budget_ms}ms)`;

  document.getElementById('reasoningBox').innerHTML = `
    <div class="async-queued-note">
      ⚙ <b>Post-Hoc Audit Path Active:</b> Response passed to consumer without blocking.<br>
      Background inspection is running asynchronously. Results will populate the audit trail automatically.
    </div>
  `;

  await refreshLog();
  runBtn.disabled = false;
  runBtn.innerHTML = `
    <svg class="play-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span>Execute Risk Inspection</span>
  `;

  await pollForResult(queued.queued_id);
}

async function pollForResult(entryId) {
  const maxAttempts = 30;
  const intervalMs = 500;

  for (let i = 0; i < maxAttempts; i++) {
    await sleep(intervalMs);
    try {
      const entry = await (await fetch(`${API}/api/audit-log/${entryId}`)).json();
      if (entry.decision && entry.decision !== 'PENDING') {
        setGaugeScore('resp', entry.responsibility_score ?? 0, 'Completed');
        setGaugeScore('perf', entry.performance_score ?? 0, 'Completed');
        setGaugeScore('cost', entry.cost_score ?? 0, 'Completed');

        renderDecisionFromAuditEntry(entry);
        await refreshLog();
        highlightLogRow(entryId);
        return;
      }
    } catch (e) { /* keep polling */ }
  }

  const badge = document.getElementById('decisionBadge');
  document.getElementById('decisionText').textContent = 'AUDIT TIMEOUT';
  document.getElementById('reasoningBox').innerHTML = `
    <div class="no-context-warn">⚠ Polling timed out — background inspection is processing. Check audit log #${entryId}.</div>
  `;
}

function renderDecisionFromAuditEntry(entry) {
  const pseudo = {
    decision: entry.decision,
    total_score: entry.total_score,
    responsibility_score: entry.responsibility_score,
    responsibility_flags: [],
    performance_score: entry.performance_score,
    performance_reasoning: entry.reasoning || '—',
    performance_method: 'llm-judge',
    performance_confidence: '—',
    performance_no_context: false,
    cost_score: entry.cost_score,
    estimated_tokens: 0,
    budget_tokens: 0,
    compound_incident: !!entry.compound_incident,
    incident_type: entry.incident_type || 'none',
    override_reason: entry.override_reason || null,
    latency_ms: entry.latency_ms,
    over_budget: !!entry.over_budget,
    fix: null,
  };
  renderDecision(pseudo);

  const reasoningBox = document.getElementById('reasoningBox');
  const existing = reasoningBox.innerHTML;
  reasoningBox.innerHTML = existing + `
    <div style="margin-top:10px;font-size:11px;color:var(--text-muted);font-family:var(--font-mono);border-top:1px dashed var(--border);padding-top:10px;">
      Stored Audit Reasoning: ${escapeHtml(entry.reasoning || '—')}
    </div>`;
}

function highlightLogRow(entryId) {
  const rows = document.querySelectorAll('#logTableWrap tr');
  for (const row of rows) {
    if (row.dataset.entryId === String(entryId)) {
      row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      row.classList.add('row-highlight');
      setTimeout(() => row.classList.remove('row-highlight'), 2000);
      break;
    }
  }
}

/* ── Shared Verdict & Telemetry Renderer ─────────────────────────────── */

function renderDecision(r) {
  const badge = document.getElementById('decisionBadge');
  badge.className = 'decision-badge ' + r.decision;

  const decisionText = document.getElementById('decisionText');
  decisionText.textContent = r.decision;

  const subtitle = {
    PASS:  'All risk checks below policy thresholds — response cleared for release.',
    FIX:   'Minor compliance or cost risk detected — auto-corrected before output.',
    HUMAN: 'Judgment call required — routed to compliance review queue.',
    BLOCK: 'High risk or compliance violation — blocked from user presentation.',
    ERROR: 'Inspection engine encountered an exception.',
  }[r.decision] || '';

  document.getElementById('decisionSub').textContent = `Total Risk Score: ${r.total_score}/100 · ${subtitle}`;

  // Latency SLA Badge
  const latencyBadge = document.getElementById('latencyBadge');
  const uc = USE_CASES[currentUC];
  if (r.latency_ms != null && latencyBadge) {
    latencyBadge.style.display = 'inline-flex';
    const over = r.over_budget;
    const color = over ? 'var(--danger)' : 'var(--safe)';
    latencyBadge.style.borderColor = color;
    latencyBadge.style.color = color;
    latencyBadge.innerHTML = `⏱ ${r.latency_ms}ms / ${uc?.latency_budget_ms ?? '?'}ms SLA ${over ? '⚠ OVER' : '✓'}`;
  }

  // Formatting Flags & Callouts
  const flagList = (r.responsibility_flags && r.responsibility_flags.length)
    ? r.responsibility_flags.map(f => `<span style="color:var(--danger)">${escapeHtml(f)}</span>`).join(', ')
    : 'No PII or bias flags detected';

  const confColor = { high: 'var(--safe)', medium: 'var(--warn)', low: 'var(--danger)' }[r.performance_confidence] || 'var(--text-muted)';
  const confBadge = r.performance_confidence !== '—'
    ? `<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:${confColor}22;color:${confColor};border:1px solid ${confColor}44;font-family:var(--font-mono);">${r.performance_confidence.toUpperCase()} CONFIDENCE</span>`
    : '';

  const overrideNote = r.override_reason
    ? `<div class="override-note">⛔ <b>HARD OVERRIDE TRIGGERED:</b> Decision forced to ${r.decision} regardless of total risk score.<br><span>${escapeHtml(r.override_reason)}</span><br><span style="font-size:10px;opacity:0.75">Weighted score (${r.total_score}/100) preserved in audit trail for model tuning.</span></div>`
    : '';

  const compoundNote = r.compound_incident
    ? `<div class="compound-note">🔗 <b>COMPOUND INCIDENT (${r.incident_type.replace(/_/g, ' ').toUpperCase()}):</b> Multiple risk detectors fired on the same turn. Corroboration boost applied.</div>`
    : '';

  const noCtxWarning = r.performance_no_context
    ? `<div class="no-context-warn">⚠ <b>PLAUSIBILITY-ONLY MODE:</b> No reference source context provided. Performance score reflects internal plausibility, not factual grounding.</div>`
    : '';

  document.getElementById('reasoningBox').innerHTML = `
    ${overrideNote}${noCtxWarning}${compoundNote}
    <div style="line-height:1.7;">
      <b>RESPONSIBILITY (${r.responsibility_score}/100):</b> ${flagList}<br>
      <b>PERFORMANCE (${r.performance_score}/100):</b> ${escapeHtml(r.performance_reasoning)} ${confBadge} <span style="color:var(--text-dim)">[${r.performance_method}]</span><br>
      <b>COST (${r.cost_score}/100):</b> ~${r.estimated_tokens} est. tokens vs. ${r.budget_tokens} budget for ${uc?.label || 'this use case'}
    </div>
  `;

  // Render Auto-Correction (FIX) Diff Panel
  const fixPanel = document.getElementById('fixPanel');
  if (r.fix) {
    fixPanel.innerHTML = `
      <div class="fix-panel">
        <div class="fix-panel-head">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="20 6 9 17 4 12"></polyline>
          </svg>
          Auto-Correction Applied — ${escapeHtml(r.fix.method)}
        </div>
        <div class="fix-row">
          <span class="fix-label">Before Correction (Raw Output)</span>
          <div class="fix-before">${escapeHtml(r.fix.before)}</div>
          <span class="fix-label">After Correction (Sanitized Output)</span>
          <div class="fix-after">${escapeHtml(r.fix.after)}</div>
        </div>
      </div>
    `;
  } else {
    fixPanel.innerHTML = '';
  }
}

/* ── Audit Trail & Metrics Renderers ──────────────────────────────────── */

async function refreshLog() {
  try {
    const [log, metrics] = await Promise.all([
      (await fetch(`${API}/api/audit-log`)).json(),
      (await fetch(`${API}/api/metrics`)).json()
    ]);
    renderMetrics(metrics);
    renderLogTable(log);
  } catch (e) {
    console.error('Log refresh error:', e);
  }
}

function renderMetrics(m) {
  const el = document.getElementById('metricsRow');
  const acc = m.reviewer_confirmed_accuracy_pct;
  const accColor = acc === null ? 'var(--text-muted)' : (acc >= 70 ? 'var(--safe)' : 'var(--warn)');
  
  el.innerHTML = `
    <div class="metric-card">
      <div class="metric-num" style="color:var(--safe)">${m.counts.PASS}</div>
      <div class="metric-lbl">Passed Clean</div>
    </div>
    <div class="metric-card">
      <div class="metric-num" style="color:var(--warn)">${m.counts.FIX}</div>
      <div class="metric-lbl">Auto-Corrected</div>
    </div>
    <div class="metric-card">
      <div class="metric-num" style="color:var(--human)">${m.counts.HUMAN}</div>
      <div class="metric-lbl">Routed to Human</div>
    </div>
    <div class="metric-card">
      <div class="metric-num" style="color:${accColor}">${acc === null ? '—' : acc + '%'}</div>
      <div class="metric-lbl">Reviewer Accuracy (${m.reviewed} reviewed)</div>
    </div>
  `;
}

function renderLogTable(log) {
  const wrap = document.getElementById('logTableWrap');
  if (!log.length) {
    wrap.innerHTML = '<div class="empty-log">No inspections logged yet — execute an inspection above to populate the audit trail.</div>';
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>Use Case</th>
          <th>Resp.</th>
          <th>Perf.</th>
          <th>Cost</th>
          <th>Total</th>
          <th>Latency SLA</th>
          <th>Decision</th>
          <th>Reviewer Action</th>
        </tr>
      </thead>
      <tbody>
        ${log.map(r => `
          <tr data-entry-id="${r.id}">
            <td>${new Date(r.created_at).toLocaleTimeString()}</td>
            <td><b>${escapeHtml(r.use_case)}</b></td>
            <td>${r.responsibility_score ?? '—'}</td>
            <td>${r.performance_score ?? '—'}</td>
            <td>${r.cost_score ?? '—'}</td>
            <td style="font-weight:700;">${r.total_score ?? '—'}</td>
            <td style="font-family:var(--font-mono);font-size:11px;color:${r.over_budget ? 'var(--danger)' : 'var(--text-muted)'}">
              ${r.latency_ms != null ? r.latency_ms + 'ms' + (r.over_budget ? ' ⚠' : '') : '—'}
            </td>
            <td><span class="badge-sm ${r.decision}">${r.decision}</span></td>
            <td>
              <div class="override-row">
                <button class="ov-btn ${r.review === 'confirm' ? 'selected-confirm' : ''}" onclick="setReview(${r.id}, 'confirm')">✓ Correct</button>
                <button class="ov-btn ${r.review === 'override' ? 'selected-override' : ''}" onclick="setReview(${r.id}, 'override')">✕ Override</button>
              </div>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

async function setReview(id, value) {
  const currentRow = document.querySelector(`button[onclick="setReview(${id}, '${value}')"]`);
  const isSelected = currentRow && currentRow.classList.contains(value === 'confirm' ? 'selected-confirm' : 'selected-override');
  const newValue = isSelected ? null : value;

  await fetch(`${API}/api/audit-log/${id}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ review: newValue })
  });
  await refreshLog();
}

async function clearLog() {
  if (confirm('Clear all audit log history?')) {
    await fetch(`${API}/api/audit-log`, { method: 'DELETE' });
    await refreshLog();
  }
}

boot();
