const API = ""; // same-origin; backend serves this frontend too

let USE_CASES = {};
let SAMPLES = {};
let currentUC = null;

async function boot(){
  try{
    const list = await (await fetch(`${API}/api/use-cases`)).json();
    USE_CASES = Object.fromEntries(list.map(uc => [uc.key, uc]));
    currentUC = list[0].key;
    setStatus(true);
  }catch(e){
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

function setStatus(ok){
  const el = document.getElementById('apiStatus');
  el.className = 'status-pill' + (ok ? '' : ' error');
  el.innerHTML = `<div class="status-dot"></div> ${ok ? 'API connected' : 'API unreachable — is the backend running?'}`;
}

function renderUseCaseTabs(){
  const row = document.getElementById('ucRow');
  row.innerHTML = '';
  Object.values(USE_CASES).forEach(uc => {
    const btn = document.createElement('div');
    btn.className = 'uc-btn' + (uc.key === currentUC ? ' active' : '');
    btn.onclick = async () => { currentUC = uc.key; renderUseCaseTabs(); await loadSamples(); renderPolicyNote(); };
    const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
    const tags = [
      `${uc.latency_budget_ms}ms budget`,
      uc.pipeline_position.split(' —')[0],
      isPostHoc ? '⚙ async' : '⚡ blocking',
    ];
    btn.innerHTML = `<div class="uc-name">${uc.label}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px;">${uc.description}</div>
      <div class="uc-meta">${tags.map(t=>`<span class="uc-tag">${t}</span>`).join('')}</div>`;
    row.appendChild(btn);
  });
}

async function loadSamples(){
  if (!SAMPLES[currentUC]){
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

function renderPolicyNote(){
  const uc = USE_CASES[currentUC];
  const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
  document.getElementById('policyNote').innerHTML =
    `<b style="color:var(--accent)">Active policy — ${uc.label}:</b> ${uc.pipeline_position}<br>
     Weights: responsibility ${Math.round(uc.weights.responsibility*100)}% · performance ${Math.round(uc.weights.performance*100)}% · cost ${Math.round(uc.weights.cost*100)}%<br>
     Thresholds: BLOCK ≥ ${uc.thresholds.block} · HUMAN ≥ ${uc.thresholds.human} · FIX ≥ ${uc.thresholds.fix}<br>
     <span style="color:${isPostHoc ? 'var(--human)' : 'var(--safe)'}">
       ${isPostHoc
         ? '⚙ Post-hoc mode — inspection runs asynchronously; HTTP response returns immediately and result lands in the audit trail.'
         : '⚡ Blocking mode — HTTP response is held until all checks (including LLM judge) complete.'}
     </span>`;
}

function resetNodes(){
  ['resp','perf','cost'].forEach(k => {
    const c = document.getElementById('circ-'+k);
    c.className = 'node-circle';
    const s = document.getElementById('score-'+k);
    s.textContent = '—'; s.style.color = 'var(--muted)';
  });
}
function setNodeActive(k){ document.getElementById('circ-'+k).classList.add('active'); }
function setNodeDone(k, score){
  const c = document.getElementById('circ-'+k);
  c.classList.remove('active');
  c.classList.add(score >= 60 ? 'done-danger' : score >= 30 ? 'done-warn' : 'done-safe');
  const s = document.getElementById('score-'+k);
  s.textContent = 'risk ' + score;
  s.style.color = score >= 60 ? 'var(--danger)' : score >= 30 ? 'var(--warn)' : 'var(--safe)';
}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function escapeHtml(str){ const d = document.createElement('div'); d.textContent = str; return d.innerHTML; }

// ── Routing: pipeline_position drives which endpoint is called ────────────────

async function runInspection(){
  const uc = USE_CASES[currentUC];
  const isPostHoc = uc.pipeline_position.toLowerCase().includes('post-hoc');
  if (isPostHoc){
    await runAsyncInspection();
  } else {
    await runSyncInspection();
  }
}

// ── Blocking inspection (chatbot / copilot) ───────────────────────────────────

async function runSyncInspection(){
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response){ alert('Paste an AI response to inspect first.'); return; }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true; runBtn.textContent = 'Inspecting…';
  resetNodes();
  document.getElementById('decisionBadge').className = 'decision-badge';
  document.getElementById('decisionBadge').textContent = 'INSPECTING…';
  document.getElementById('reasoningBox').innerHTML = '';
  document.getElementById('fixPanel').innerHTML = '';

  // Light up nodes progressively while the real API call is in flight
  setNodeActive('resp'); await sleep(150);
  setNodeActive('perf'); await sleep(150);
  setNodeActive('cost');

  let result;
  try{
    const res = await fetch(`${API}/api/inspect`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ use_case: currentUC, question, context, response })
    });
    if (!res.ok){ throw new Error((await res.json()).detail || 'Request failed'); }
    result = await res.json();
  }catch(e){
    document.getElementById('decisionBadge').textContent = 'ERROR';
    document.getElementById('reasoningBox').textContent = 'Request failed: ' + e.message;
    runBtn.disabled = false; runBtn.textContent = '▶ Run inspection';
    return;
  }

  setNodeDone('resp', result.responsibility_score);
  setNodeDone('perf', result.performance_score);
  setNodeDone('cost', result.cost_score);
  renderDecision(result);
  await refreshLog();

  runBtn.disabled = false; runBtn.textContent = '▶ Run inspection';
}

// ── Post-hoc async inspection (decision) ─────────────────────────────────────

async function runAsyncInspection(){
  const question = document.getElementById('question').value.trim();
  const context  = document.getElementById('context').value.trim();
  const response = document.getElementById('response').value.trim();
  if (!response){ alert('Paste an AI response to inspect first.'); return; }

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true; runBtn.textContent = 'Queueing…';
  resetNodes();

  // Show all nodes as "queued" (pulsing active) immediately — they'll run
  // in the background and we won't get per-node callbacks, but the animation
  // communicates that something is happening without blocking the UI.
  setNodeActive('resp'); setNodeActive('perf'); setNodeActive('cost');

  const badge = document.getElementById('decisionBadge');
  badge.className = 'decision-badge';
  badge.textContent = 'QUEUING…';
  document.getElementById('reasoningBox').innerHTML = '';
  document.getElementById('fixPanel').innerHTML = '';

  let queued;
  try{
    const res = await fetch(`${API}/api/inspect-async`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ use_case: currentUC, question, context, response })
    });
    if (!res.ok){ throw new Error((await res.json()).detail || 'Request failed'); }
    queued = await res.json();
  }catch(e){
    badge.textContent = 'ERROR';
    document.getElementById('reasoningBox').textContent = 'Request failed: ' + e.message;
    runBtn.disabled = false; runBtn.textContent = '▶ Run inspection';
    return;
  }

  // HTTP response returned immediately — show the "queued" state.
  badge.className = 'decision-badge HUMAN';  // neutral blue while pending
  badge.innerHTML = `QUEUED <div class="decision-sub">ID #${queued.queued_id} · post-hoc inspection running in background · budget ${queued.latency_budget_ms}ms</div>`;
  document.getElementById('reasoningBox').innerHTML =
    `<div class="async-queued-note">⚙ Inspection dispatched to background task — this is the post-hoc audit path.<br>
     The primary response is <b>not blocked</b>. Result will appear in the audit trail below once the LLM judge completes.</div>`;

  // Refresh immediately so the PENDING row appears in the log
  await refreshLog();
  runBtn.disabled = false; runBtn.textContent = '▶ Run inspection';

  // Poll for completion and update the badge + audit log when ready
  await pollForResult(queued.queued_id);
}

async function pollForResult(entryId){
  const maxAttempts = 25;   // ~15 seconds max
  const intervalMs  = 600;

  for (let i = 0; i < maxAttempts; i++){
    await sleep(intervalMs);
    try{
      const entry = await (await fetch(`${API}/api/audit-log/${entryId}`)).json();
      if (entry.decision && entry.decision !== 'PENDING'){
        // Inspection complete — update the node indicators
        setNodeDone('resp', entry.responsibility_score ?? 0);
        setNodeDone('perf', entry.performance_score ?? 0);
        setNodeDone('cost', entry.cost_score ?? 0);

        // Render a result from the audit log entry
        renderDecisionFromAuditEntry(entry);
        await refreshLog();
        highlightLogRow(entryId);
        return;
      }
    }catch(e){ /* keep polling */ }
  }
  // Timed out — tell the user to check the audit log manually
  const badge = document.getElementById('decisionBadge');
  badge.textContent = 'TIMEOUT';
  document.getElementById('reasoningBox').innerHTML =
    `<div class="no-context-warn">⚠ Polling timed out — the inspection may still be running. Check the audit trail for entry #${entryId}.</div>`;
}

function renderDecisionFromAuditEntry(entry){
  // Builds a display-compatible object from an audit log row.
  // The audit log now stores override_reason, compound_incident, incident_type,
  // latency_ms, over_budget — enough to reconstruct the key callouts.
  const pseudo = {
    decision: entry.decision,
    total_score: entry.total_score,
    responsibility_score: entry.responsibility_score,
    responsibility_flags: [],   // not separately stored; shown via reasoning string
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
  // Replace the performance reasoning row with the full stored reasoning string
  // (which already contains flags, confidence, cost, latency, override notes)
  const reasoningBox = document.getElementById('reasoningBox');
  const existing = reasoningBox.innerHTML;
  reasoningBox.innerHTML = existing + `
    <div style="margin-top:8px;font-size:11px;color:var(--muted);font-family:var(--mono);border-top:1px dashed var(--border);padding-top:8px;">
      Full audit reasoning: ${escapeHtml(entry.reasoning || '—')}
    </div>`;
}

function highlightLogRow(entryId){
  // Briefly flash the relevant audit log row after polling completes
  const rows = document.querySelectorAll('#logTableWrap tr');
  for (const row of rows){
    if (row.dataset.entryId === String(entryId)){
      row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      row.classList.add('row-highlight');
      setTimeout(() => row.classList.remove('row-highlight'), 2000);
      break;
    }
  }
}

// ── Shared decision rendering ─────────────────────────────────────────────────

function renderDecision(r){
  const badge = document.getElementById('decisionBadge');
  badge.className = 'decision-badge ' + r.decision;
  const subtitle = {
    PASS:  'Below all thresholds — sent straight through.',
    FIX:   'Minor issue — auto-corrected and sent through.',
    HUMAN: 'Judgment call — routed to a reviewer.',
    BLOCK: 'Clearly unsafe or non-compliant — stopped before the user sees it.',
    ERROR: 'Background inspection failed — see server logs.',
  }[r.decision] || '';

  const uc = USE_CASES[currentUC];
  const latencyHtml = (r.latency_ms != null)
    ? (() => {
        const over = r.over_budget;
        const color = over ? 'var(--danger)' : 'var(--safe)';
        return `<span style="font-size:11px;font-family:var(--mono);color:${color};margin-left:8px;">⏱ ${r.latency_ms}ms / ${uc?.latency_budget_ms ?? '?'}ms budget${over ? ' ⚠ OVER' : ' ✓'}</span>`;
      })()
    : '';

  badge.innerHTML = `${r.decision} ${latencyHtml}<div class="decision-sub">risk score ${r.total_score}/100 · ${subtitle}</div>`;

  const flagList = (r.responsibility_flags && r.responsibility_flags.length)
    ? r.responsibility_flags.join(', ')
    : 'none detected';

  const confColor = { high: 'var(--safe)', medium: 'var(--warn)', low: 'var(--danger)' }[r.performance_confidence] || 'var(--muted)';
  const confBadge = r.performance_confidence !== '—'
    ? `<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:${confColor}22;color:${confColor};border:1px solid ${confColor}44;">${r.performance_confidence} confidence</span>`
    : '';

  const noCtxWarning = r.performance_no_context
    ? `<div class="no-context-warn">⚠ No source context — performance score reflects plausibility only, not grounding against facts. Treat with additional caution.</div>`
    : '';

  const compoundNote = r.compound_incident
    ? `<div class="compound-note">🔗 Compound incident detected (<b>${r.incident_type.replace(/_/g, ' ')}</b>) — both checks fired on the same event. Corroboration boost applied to total score.</div>`
    : '';

  const overrideNote = r.override_reason
    ? `<div class="override-note">⛔ Hard override — decision forced regardless of weighted score.<br><span style="opacity:0.85">${r.override_reason}</span><br><span style="font-size:10px;opacity:0.6">Weighted score (${r.total_score}/100) is preserved in the audit log for reviewer context.</span></div>`
    : '';

  document.getElementById('reasoningBox').innerHTML = `
    ${overrideNote}${noCtxWarning}${compoundNote}
    <b>Responsibility (${r.responsibility_score}):</b> ${flagList}<br>
    <b>Performance (${r.performance_score}):</b> ${r.performance_reasoning} ${confBadge} <span style="color:var(--muted)">[${r.performance_method}]</span><br>
    <b>Cost (${r.cost_score}):</b> ~${r.estimated_tokens} est. tokens vs. ${r.budget_tokens} budget for this use case
  `;

  const fixPanel = document.getElementById('fixPanel');
  if (r.fix){
    fixPanel.innerHTML = `<div class="fix-panel">
      <div class="fix-panel-head">Auto-correction applied — ${r.fix.method}</div>
      <div class="fix-row">
        <span class="fix-label">Before</span>
        <div class="fix-before">${escapeHtml(r.fix.before)}</div>
        <span class="fix-label">After (sent to user)</span>
        <div class="fix-after">${escapeHtml(r.fix.after)}</div>
      </div>
    </div>`;
  } else {
    fixPanel.innerHTML = '';
  }
}

// ── Audit log ─────────────────────────────────────────────────────────────────

async function refreshLog(){
  const [log, metrics] = await Promise.all([
    (await fetch(`${API}/api/audit-log`)).json(),
    (await fetch(`${API}/api/metrics`)).json()
  ]);
  renderMetrics(metrics);
  renderLogTable(log);
}

function renderMetrics(m){
  const el = document.getElementById('metricsRow');
  const acc = m.reviewer_confirmed_accuracy_pct;
  el.innerHTML = `
    <div class="metric-card"><div class="metric-num" style="color:var(--safe)">${m.counts.PASS}</div><div class="metric-lbl">Passed clean</div></div>
    <div class="metric-card"><div class="metric-num" style="color:var(--warn)">${m.counts.FIX}</div><div class="metric-lbl">Auto-fixed</div></div>
    <div class="metric-card"><div class="metric-num" style="color:var(--human)">${m.counts.HUMAN}</div><div class="metric-lbl">Sent to human</div></div>
    <div class="metric-card"><div class="metric-num" style="color:${acc===null?'var(--muted)':(acc>=70?'var(--safe)':'var(--warn)')}">${acc===null?'—':acc+'%'}</div><div class="metric-lbl">Reviewer-confirmed accuracy (${m.reviewed} reviewed)</div></div>
  `;
}

function renderLogTable(log){
  const wrap = document.getElementById('logTableWrap');
  if (!log.length){
    wrap.innerHTML = '<div class="empty-log">No inspections yet — run one above to populate the audit trail.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr><th>Time</th><th>Use case</th><th>Resp.</th><th>Perf.</th><th>Cost</th><th>Total</th><th>Latency</th><th>Decision</th><th>Reviewer feedback</th></tr></thead>
    <tbody>
      ${log.map(r => `<tr data-entry-id="${r.id}">
        <td>${new Date(r.created_at).toLocaleTimeString()}</td><td>${r.use_case}</td>
        <td>${r.responsibility_score ?? '—'}</td><td>${r.performance_score ?? '—'}</td><td>${r.cost_score ?? '—'}</td><td>${r.total_score ?? '—'}</td>
        <td style="font-family:var(--mono);font-size:11px;color:${r.over_budget ? 'var(--warn)' : 'var(--muted)'}">
          ${r.latency_ms != null ? r.latency_ms + 'ms' + (r.over_budget ? ' ⚠' : '') : '—'}
        </td>
        <td><span class="badge-sm ${r.decision}">${r.decision}</span></td>
        <td>
          <div class="override-row">
            <button class="ov-btn ${r.review==='confirm'?'selected-confirm':''}" onclick="setReview(${r.id},'confirm')">✓ correct</button>
            <button class="ov-btn ${r.review==='override'?'selected-override':''}" onclick="setReview(${r.id},'override')">✕ override</button>
          </div>
        </td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

async function setReview(id, value){
  const currentRow = document.querySelector(`button[onclick="setReview(${id},'${value}')"]`);
  const isSelected = currentRow && currentRow.classList.contains(value === 'confirm' ? 'selected-confirm' : 'selected-override');
  const newValue = isSelected ? null : value;
  await fetch(`${API}/api/audit-log/${id}/review`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ review: newValue })
  });
  await refreshLog();
}

async function clearLog(){
  await fetch(`${API}/api/audit-log`, { method: 'DELETE' });
  await refreshLog();
}

boot();
