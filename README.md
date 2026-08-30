# ControlPlane

🌐 **Live Console:** [https://cpanai-production.up.railway.app/#console](https://cpanai-production.up.railway.app/#console)

A real-time risk inspection layer for enterprise AI systems. Every AI response
passes through three checks (responsibility/PII & bias, performance/groundedness,
cost) before it reaches a person, gets a weighted risk score, and is routed
to **PASS / FIX / HUMAN / BLOCK** based on a policy specific to the use case
it came from.

## Why three checks, and why they're weighted differently per use case

Real enterprises run AI across use cases with very different risk profiles —
a customer chatbot needs sub-second blocking checks; an internal copilot can
tolerate a few seconds and cares more about hallucination than PII; a
regulated decision-support tool needs the strictest thresholds, post-hoc audit, and
mandatory human sign-off. `app/config.py` encodes this directly: same checks, different
weights, thresholds, and execution modes per use case.

## Core Architectural Guarantees

1. **Structured LLM-as-Judge & Confidence Invariants:** The performance check (`gemini-3.1-flash-lite`) returns a structured JSON payload containing `groundedness_score`, `confidence`, `safety_concern`, and `reasoning`. Without source context, confidence is capped at `"medium"` (plausibility-only mode). Low-confidence scores cannot trigger hard overrides.
2. **Hard-Override Layer:** Independent of weighted scoring, high-confidence PII (dashed SSN, 16-digit card), protected-characteristic flags in regulated lending, and structured security/credential fabrications force a deterministic **BLOCK** decision while preserving `total_score` in audit logs.
3. **Compound Incident Corroboration:** When PII/bias and hallucination fire simultaneously on the same response, ControlPlane applies a 15% corroboration boost to total risk score rather than double-counting or discounting.
4. **Three-Dimensional Auto-Fixes (`app/fixes.py`):**
   - Responsibility driving risk -> PII redaction (`[SSN REDACTED]`, etc.)
   - Performance driving risk -> Appends verification caveat disclaimer
   - Cost driving risk -> Word-count trim with truncation notice
5. **Multi-Turn Session Ledger & Agentic Actions (`app/session.py`):**
   - Stateful risk tracking with exponential decay (`decay=0.7`) across sequential turns.
   - 3-consecutive-flag streak escalation triggering mandatory human review.
   - Irreversible agentic tool executions (`action_reversible=False`) enforce a mandatory `HUMAN` review floor.
6. **Execution Pipelines & Latency Budget SLA:**
   - **Pre-response Gate (blocking):** `POST /api/inspect` runs checks concurrently (`asyncio.gather`), measuring wall-clock execution against `latency_budget_ms`.
   - **Post-hoc Audit (async):** `POST /api/inspect-async` returns immediately with a queued ID for non-blocking post-hoc pipelines, running inspection in a background task.

## Architecture

```
frontend/              static UI, calls API, handles polling for async post-hoc inspections
app/
  config.py            per-use-case policy: weights, thresholds, latency budgets, token limits
  checks/
    responsibility.py  PII + bias regex detection (fast, deterministic structural patterns)
    performance.py     LLM-as-judge groundedness, safety & semantic bias via gemini-3.1-flash-lite
    cost.py            token estimate vs. per-use-case budget (60-token baseline)
  engine.py            runs checks concurrently, compound scoring, hard-override rules
  session.py           multi-turn risk decay ledger, streak escalation, irreversible action tracking
  fixes.py             real auto-correction for FIX decisions (PII redaction, caveat, cost trim)
  storage.py           SQLite audit log with migrations & reviewer feedback metrics
  samples.py           canned demo scenarios per use case
  main.py              FastAPI app with sync/async endpoints and background tasks
test_suite_75.py       comprehensive 75-case feature and regression benchmark suite
build_pdf_75.py        automated benchmark runner and 12-column ReportLab PDF generator
```

## Running Locally

### 1. Install Dependencies

```bash
cd controlplane
pip install -r requirements.txt
```

### 2. Configure Gemini API Key (Recommended)

Set your Google AI Studio API key as an environment variable or in a `.env` file:

**macOS / Linux:**
```bash
export GEMINI_API_KEY="your_api_key_here"
```

**Windows PowerShell:**
```powershell
$env:GEMINI_API_KEY="your_api_key_here"
```

> *Note: If no API key is provided, the engine gracefully falls back to deterministic heuristic scans.*

### 3. Start the FastAPI Server

```bash
uvicorn app.main:app --reload --port 8000
```

Open **`http://127.0.0.1:8000`** in your browser — the FastAPI app serves the interactive web console directly.

---

## Running the 75-Case Benchmark Suite

Verify the complete feature set (single turns, multi-turn decay, agentic action gates, feedback loops):

```bash
# Run automated test suite against local server
python test_suite_75.py --base-url http://127.0.0.1:8000

# Run full benchmark and compile 7-page PDF report
python build_pdf_75.py
```

---

## API Endpoints

| Endpoint | Method | Description |
|---|:---:|---|
| `/api/use-cases` | `GET` | List all configured use case policies, weights, and thresholds |
| `/api/samples/{use_case}` | `GET` | Canned demo scenarios for a specific use case |
| `/api/inspect` | `POST` | Blocking inspection pipeline (pre-response gate with session support) |
| `/api/inspect-async` | `POST` | Asynchronous post-hoc audit pipeline (queued background execution) |
| `/api/audit-log` | `GET` | Full inspection history |
| `/api/audit-log/{id}` | `GET` | Poll single audit log entry by ID |
| `/api/audit-log/{id}/review` | `POST` | Reviewer confirms or overrides an engine decision |
| `/api/metrics` | `GET` | Decision counts + reviewer-confirmed accuracy metrics |
| `/api/metrics/override-patterns` | `GET` | Read-only aggregation of reviewer overrides by reason & category |
| `/api/metrics/tuning-suggestions` | `GET` | Statistically gated policy calibration suggestions ($N \ge 5$, rate $\ge 30\%$) |
