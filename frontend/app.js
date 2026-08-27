const API = ""; // same-origin FastAPI backend

let USE_CASES = {};
let SAMPLES = {};
let currentUC = null;

const CIRCUMFERENCE = 238.76; // 2 * PI * 38 for SVG radial gauge ring

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

  renderSegmentedPolicyControl();
  await loadSamples();
  renderPolicyNote();
  await refreshLog();

  document.getElementById('runBtn').onclick = runInspection;
  document.getElementById('clearBtn').onclick = clearLog;

  // Keyboard shortcut listener: Cmd/Ctrl + Enter triggers inspection
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      runInspection();
    }
  });
}

function setStatus(ok) {
  const el = document.getElementById('apiStatus');
  const txt = document.getElementById('statusText');
  el.className = 'status-pill' + (ok ? '' : ' error');
  txt.textContent = ok ? 'Connected' : 'Offline';
}

function renderSegmentedPolicyControl() {
  const row = document.getElementById('ucRow');
  row.innerHTML = '';
  Object.values(USE_CASES).forEach(uc => {
    const btn = document.createElement('button');
    btn.className = 'seg-btn' + (uc.key === currentUC ? ' active' : '');
    btn.onclick = async () => {
      currentUC = uc.key;
      renderSegmentedPolicyControl();
      await loadSamples();
      renderPolicyNote();
      resetGauges();
    };
    btn.innerHTML = `
      <div class="seg-title">
        <span>${escapeHtml(uc.label)}</span>
        <span class="seg-sla">${uc.latency_budget_ms}ms SLA</span>
      </div>
      <div class="seg-desc">${escapeHtml(uc.description)}</div>
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
    b.className = 'preset-btn';
    b.textContent = s.label;
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
  document.getElementById('policyNote').innerHTML = `
    <b>Active Context (${uc.label}):</b> ${escapeHtml(uc.pipeline_position)} · 
    <b>Weights:</b> Resp ${Math.round(uc.weights.responsibility * 100)}% / Perf ${Math.round(uc.weights.performance * 100)}% / Cost ${Math.round(uc.weights.cost * 100)}% · 
    <b>Action Gates:</b> BLOCK ≥ ${uc.thresholds.block} · HUMAN ≥ ${uc.thresholds.human} · FIX ≥ ${uc.thresholds.fix}
  `;
}

/* ── SVG Radial Gauge Controller ──────────────────────────────────────── */

function resetGauges() {
  ['resp', 'perf', 'cost'].forEach(k => {
    const ring = document.getElementById('ring-' + k);
    const val = document.getElementById('val-' + k);
    const sub = document.getElementById('sub-' + k);
    if (ring) {
      ring.style.strokeDashoffset = CIRCUMFERENCE;
    }
    if (val) val.textContent = '0';
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

  if (ring) {
    const offset = CIRCUMFERENCE - (clamped / 100) * CIRCUMFERENCE;
    ring.style.strokeDashoffset = offset;
  }

  if (val) {
    val.textContent = clamped;
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

/* ── Synchronous Inspection (Chatbot / Copilot) ───────────────────────── */

async function runSyncInspection() {
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response) {
    alert('Please enter an AI response to inspect.');
    return;
  }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  runBtn.innerHTML = `<span>Inspecting Payload…</span>`;

  resetGauges();
  const badge = document.getElementById('decisionBadge');
  badge.className = 'verdict-banner IDLE';
  document.getElementById('decisionText').textContent = 'EVALUATING PIPELINE…';
  document.getElementById('decisionSub').textContent = 'Running Responsibility, Performance, and Cost checks concurrently...';
  document.getElementById('reasoningBox').innerHTML = '<div class="reasoning-empty">Executing policy evaluation...</div>';
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
    badge.className = 'verdict-banner BLOCK';
    document.getElementById('decisionText').textContent = 'ERROR';
    document.getElementById('decisionSub').textContent = 'Inspection API Exception: ' + e.message;
    document.getElementById('reasoningBox').innerHTML = `<div class="callout-override">❌ Error: ${escapeHtml(e.message)}</div>`;
    runBtn.disabled = false;
    runBtn.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="btn-icon">
        <polygon points="5 3 19 12 5 21 5 3"/>
      </svg>
      <span>Run Policy Inspection</span>
      <span class="kbd-hint">⌘Enter</span>
    `;
    return;
  }

  // Update Gauges
  setGaugeScore('resp', result.responsibility_score, `${result.responsibility_flags.length} flag(s)`);
  setGaugeScore('perf', result.performance_score, `${result.performance_confidence} conf.`);
  setGaugeScore('cost', result.cost_score, `~${result.estimated_tokens} tokens`);

  renderDecision(result);
  await refreshLog();

  runBtn.disabled = false;
  runBtn.innerHTML = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="btn-icon">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span>Run Policy Inspection</span>
    <span class="kbd-hint">⌘Enter</span>
  `;
}

/* ── Asynchronous Inspection (Decision-Support) ──────────────────────── */

async function runAsyncInspection() {
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response) {
    alert('Please enter an AI response to inspect.');
    return;
  }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  runBtn.innerHTML = `<span>Queueing Audit Task…</span>`;
  resetGauges();

  const badge = document.getElementById('decisionBadge');
  badge.className = 'verdict-banner IDLE';
  document.getElementById('decisionText').textContent = 'DISPATCHING ASYNC TASK…';
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
    badge.className = 'verdict-banner BLOCK';
    document.getElementById('decisionText').textContent = 'ERROR';
    document.getElementById('decisionSub').textContent = 'Request failed: ' + e.message;
    runBtn.disabled = false;
    runBtn.innerHTML = `<span>Run Policy Inspection</span>`;
    return;
  }

  badge.className = 'verdict-banner HUMAN';
  document.getElementById('decisionText').textContent = 'AUDIT TASK QUEUED';
  document.getElementById('decisionSub').textContent = `Task ID #${queued.queued_id} dispatched to background queue (SLA Budget: ${queued.latency_budget_ms}ms)`;

  document.getElementById('reasoningBox').innerHTML = `
    <div class="callout-compound">
      ⚙ <b>Post-Hoc Audit Path Dispatched:</b> Response returned to consumer without blocking.<br>
      Background evaluation is running. Results will populate the audit log automatically.
    </div>
  `;

  await refreshLog();
  runBtn.disabled = false;
  runBtn.innerHTML = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="btn-icon">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span>Run Policy Inspection</span>
    <span class="kbd-hint">⌘Enter</span>
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
        return;
      }
    } catch (e) { /* keep polling */ }
  }

  const badge = document.getElementById('decisionBadge');
  document.getElementById('decisionText').textContent = 'AUDIT TIMEOUT';
  document.getElementById('reasoningBox').innerHTML = `
    <div class="callout-nocontext">⚠ Polling timed out — background inspection is processing. Check audit log #${entryId}.</div>
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
}

/* ── Shared Verdict & Telemetry Renderer ─────────────────────────────── */

function renderDecision(r) {
  const badge = document.getElementById('decisionBadge');
  badge.className = 'verdict-banner ' + r.decision;

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
    latencyBadge.innerHTML = `⏱ <span style="font-family:var(--font-mono);">${r.latency_ms}ms / ${uc?.latency_budget_ms ?? '?'}ms SLA</span> ${over ? '⚠ OVER' : '✓'}`;
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
    ? `<div class="callout-override">⛔ <b>HARD OVERRIDE TRIGGERED:</b> Decision forced to ${r.decision} regardless of total risk score.<br><span>${escapeHtml(r.override_reason)}</span></div>`
    : '';

  const compoundNote = r.compound_incident
    ? `<div class="callout-compound">🔗 <b>COMPOUND INCIDENT (${r.incident_type.replace(/_/g, ' ').toUpperCase()}):</b> Multiple risk detectors fired on the same turn. Corroboration boost applied.</div>`
    : '';

  const noCtxWarning = r.performance_no_context
    ? `<div class="callout-nocontext">⚠ <b>PLAUSIBILITY-ONLY MODE:</b> No reference source context provided. Performance score reflects internal plausibility, not factual grounding.</div>`
    : '';

  document.getElementById('reasoningBox').innerHTML = `
    ${overrideNote}${noCtxWarning}${compoundNote}
    <div>
      <b>RESPONSIBILITY (${r.responsibility_score}/100):</b> ${flagList}<br>
      <b>PERFORMANCE (${r.performance_score}/100):</b> ${escapeHtml(r.performance_reasoning)} ${confBadge} <span style="color:var(--text-dim)">[${r.performance_method}]</span><br>
      <b>COST (${r.cost_score}/100):</b> ~${r.estimated_tokens} est. tokens vs. ${r.budget_tokens} budget for ${escapeHtml(uc?.label || 'this use case')}
    </div>
  `;

  // Render Auto-Correction (FIX) Diff Box
  const fixPanel = document.getElementById('fixPanel');
  if (r.fix) {
    fixPanel.innerHTML = `
      <div class="fix-box">
        <div class="fix-header">
          Auto-Correction Applied — ${escapeHtml(r.fix.method)}
        </div>
        <div class="fix-body">
          <span class="fix-tag">Raw Output</span>
          <div class="fix-before">${escapeHtml(r.fix.before)}</div>
          <span class="fix-tag">Sanitized Output</span>
          <div class="fix-after">${escapeHtml(r.fix.after)}</div>
        </div>
      </div>
    `;
  } else {
    fixPanel.innerHTML = '';
  }
}

/* ── Audit Log & Metrics Renderers ──────────────────────────────────── */

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
      <div class="metric-val" style="color:var(--safe)">${m.counts.PASS}</div>
      <div class="metric-lbl">Passed Clean</div>
    </div>
    <div class="metric-card">
      <div class="metric-val" style="color:var(--warn)">${m.counts.FIX}</div>
      <div class="metric-lbl">Auto-Corrected</div>
    </div>
    <div class="metric-card">
      <div class="metric-val" style="color:var(--human)">${m.counts.HUMAN}</div>
      <div class="metric-lbl">Routed to Human</div>
    </div>
    <div class="metric-card">
      <div class="metric-val" style="color:${accColor}">${acc === null ? '—' : acc + '%'}</div>
      <div class="metric-lbl">Reviewer Accuracy (${m.reviewed} reviewed)</div>
    </div>
  `;
}

function renderLogTable(log) {
  const wrap = document.getElementById('logTableWrap');
  if (!log.length) {
    wrap.innerHTML = '<div class="empty-state">No audit logs recorded. Execute an inspection above to populate telemetry.</div>';
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Use Case</th>
          <th>Resp.</th>
          <th>Perf.</th>
          <th>Cost</th>
          <th>Total</th>
          <th>Latency SLA</th>
          <th>Verdict</th>
          <th>Reviewer Action</th>
        </tr>
      </thead>
      <tbody>
        ${log.map(r => `
          <tr data-entry-id="${r.id}">
            <td style="font-family:var(--font-mono);font-size:11px;">${new Date(r.created_at).toLocaleTimeString()}</td>
            <td><b>${escapeHtml(r.use_case)}</b></td>
            <td style="font-family:var(--font-mono);">${r.responsibility_score ?? '—'}</td>
            <td style="font-family:var(--font-mono);">${r.performance_score ?? '—'}</td>
            <td style="font-family:var(--font-mono);">${r.cost_score ?? '—'}</td>
            <td style="font-family:var(--font-mono);font-weight:700;">${r.total_score ?? '—'}</td>
            <td style="font-family:var(--font-mono);font-size:11px;color:${r.over_budget ? 'var(--danger)' : 'var(--text-muted)'}">
              ${r.latency_ms != null ? r.latency_ms + 'ms' + (r.over_budget ? ' ⚠' : '') : '—'}
            </td>
            <td><span class="badge-verdict ${r.decision}">${r.decision}</span></td>
            <td>
              <div class="review-actions">
                <button class="btn-review ${r.review === 'confirm' ? 'selected-confirm' : ''}" onclick="setReview(${r.id}, 'confirm')">✓ Correct</button>
                <button class="btn-review ${r.review === 'override' ? 'selected-override' : ''}" onclick="setReview(${r.id}, 'override')">✕ Override</button>
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
  if (confirm('Clear all recorded audit logs?')) {
    await fetch(`${API}/api/audit-log`, { method: 'DELETE' });
    await refreshLog();
  }
}

boot();
