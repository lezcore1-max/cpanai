# ControlPlane

A real-time risk inspection layer for enterprise AI systems. Every AI response
passes through three checks (responsibility/PII, performance/hallucination,
cost) before it reaches a person, gets a weighted risk score, and is routed
to **PASS / FIX / HUMAN / BLOCK** based on a policy specific to the use case
it came from.

## Why three checks, and why they're weighted differently per use case

Real enterprises run AI across use cases with very different risk profiles —
a customer chatbot needs sub-second, blocking checks; an internal copilot can
tolerate a few seconds and cares more about hallucination than PII; a
regulated decision-support tool needs the strictest thresholds and mandatory
human sign-off. `app/config.py` encodes this directly: same checks, different
weights and thresholds per use case, so the *same* flagged response can be
BLOCKED in one context and only FLAGGED in another.

## Architecture

```
frontend/            static UI, calls the API, no scoring logic client-side
app/
  config.py          per-use-case policy: weights, thresholds, cost budgets
  checks/
    responsibility.py  PII + bias regex detection (fast, deterministic)
    performance.py     LLM-as-judge groundedness check (real Gemini API call via
                        gemini-3.1-flash-lite, falls back to a heuristic if no key / call fails)
    cost.py             token estimate vs. per-use-case budget
  engine.py           runs all 3 checks concurrently, combines into a score,
                       decides the routing action
  fixes.py            real auto-correction for FIX decisions (PII redaction
                       or cost trimming — not just a label)
  storage.py           SQLite audit log with reviewer confirm/override,
                       feeds the accuracy metric
  samples.py           canned demo scenarios per use case
  main.py              FastAPI app wiring it all together
```

Checks run concurrently (`asyncio.gather` in `engine.py`) so wall-clock
latency is closer to the slowest check (the LLM call) rather than the sum of
all three — this matters for the pre-response-gate use cases where latency
budget is tight.

## Running it

```bash
cd controlplane
pip install -r requirements.txt

# optional but recommended — without it, the performance check
# falls back to a much weaker heuristic instead of calling Gemini
export GEMINI_API_KEY=AIza...

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — the FastAPI app serves the frontend directly, no
separate server needed.

## API

| Endpoint | Description |
|---|---|
| `GET /api/use-cases` | List all configured use case policies |
| `GET /api/samples/{use_case}` | Canned demo scenarios for a use case |
| `POST /api/inspect` | Run a response through the full pipeline |
| `GET /api/audit-log` | Full inspection history |
| `POST /api/audit-log/{id}/review` | Reviewer confirms/overrides a decision |
| `GET /api/metrics` | Decision counts + reviewer-confirmed accuracy |

## What's a stand-in for a real deployment

- **SQLite** → would be a proper event store / warehouse table
- **Token-count cost estimate** → would use the actual tokenizer for the model in use
- **Regex bias detection** → would be a proper fairness classifier + embedding-based anomaly detection, this covers a narrow set of patterns illustratively
- **Policy config as a Python dict** → would be a config service, editable per team/geography without a deploy
