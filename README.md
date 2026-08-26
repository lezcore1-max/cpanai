# ControlPlane

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
5. **Execution Pipelines & Latency Budget SLA:**
   - **Pre-response Gate (blocking):** `POST /api/inspect` runs checks concurrently (`asyncio.gather`), measuring wall-clock execution against `latency_budget_ms`.
   - **Post-hoc Audit (async):** `POST /api/inspect-async` returns immediately with a queued ID for non-blocking post-hoc pipelines, running inspection in a background task.

## Architecture

```
frontend/              static UI, calls API, handles polling for async post-hoc inspections
app/
  config.py            per-use-case policy: weights, thresholds, latency budgets, token limits
  checks/
    responsibility.py  PII + bias regex detection (fast, deterministic)
    performance.py     LLM-as-judge groundedness & safety check via gemini-3.1-flash-lite
    cost.py            token estimate vs. per-use-case budget
  engine.py            runs checks concurrently, compound scoring, hard-override rules
  fixes.py             real auto-correction for FIX decisions (PII, hallucination caveat, cost trim)
  storage.py          SQLite audit log with migrations & reviewer feedback metrics
  samples.py          canned demo scenarios per use case
  main.py             FastAPI app with sync/async endpoints and background tasks
```

## Running it

```bash
cd controlplane
pip install -r requirements.txt

# optional but recommended — without it, the performance check
# falls back to a heuristic scan instead of calling Gemini
export GEMINI_API_KEY=AIza...

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — the FastAPI app serves the frontend directly.

## API

| Endpoint | Description |
|---|---|
| `GET /api/use-cases` | List all configured use case policies |
| `GET /api/samples/{use_case}` | Canned demo scenarios for a use case |
| `POST /api/inspect` | Blocking inspection pipeline (pre-response gate) |
| `POST /api/inspect-async` | Asynchronous post-hoc audit pipeline (queued background execution) |
| `GET /api/audit-log` | Full inspection history |
| `GET /api/audit-log/{id}` | Poll single audit log entry by ID |
| `POST /api/audit-log/{id}/review` | Reviewer confirms/overrides a decision |
| `GET /api/metrics` | Decision counts + reviewer-confirmed accuracy |
