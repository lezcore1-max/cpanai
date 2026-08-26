from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import storage
from app.config import USE_CASES, get_policy
from app.engine import inspect
from app.fixes import apply_fix
from app.models import (
    AsyncInspectResponse,
    FixOut,
    InspectRequest,
    InspectResponse,
    ReviewRequest,
)
from app.samples import SAMPLES

app = FastAPI(title="ControlPlane API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    storage.init_db()


@app.get("/api/use-cases")
def get_use_cases():
    """List all configured use cases and their policies."""
    return [policy.model_dump() for policy in USE_CASES.values()]


@app.get("/api/use-cases/{use_case}")
def get_use_case(use_case: str):
    try:
        return get_policy(use_case).model_dump()
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown use case: {use_case}")


@app.get("/api/samples/{use_case}")
def get_samples(use_case: str):
    if use_case not in SAMPLES:
        raise HTTPException(status_code=404, detail=f"Unknown use case: {use_case}")
    return SAMPLES[use_case]


# ── Shared helper ─────────────────────────────────────────────────────────────

def _build_reasoning(result, policy) -> str:
    parts = [
        f"Responsibility: {', '.join(result.responsibility.flags) or 'no flags'}",
        f"Performance [{result.performance.confidence} confidence]: {result.performance.reasoning}",
        f"Cost: ~{result.cost.estimated_tokens} est. tokens vs {result.cost.budget_tokens} budget",
        f"Latency: {result.latency_ms}ms vs {policy.latency_budget_ms}ms budget"
        + (" [OVER BUDGET]" if result.over_budget else ""),
    ]
    if result.compound_incident:
        parts.append(
            f"Compound incident ({result.incident_type}) — corroboration boost applied"
        )
    if result.override_reason:
        parts.insert(0, f"OVERRIDE: {result.override_reason}")
    return " | ".join(parts)


def _build_fix(payload: InspectRequest, result) -> FixOut | None:
    if result.decision != "FIX":
        return None
    fix_result = apply_fix(payload.response, result.responsibility, result.cost)
    if fix_result:
        return FixOut(method=fix_result.method, before=fix_result.before, after=fix_result.after)
    return None


# ── Synchronous inspection (chatbot / copilot — blocking / parallel) ──────────

@app.post("/api/inspect", response_model=InspectResponse)
async def inspect_response(payload: InspectRequest):
    """
    Run a response through the full inspection pipeline synchronously.
    Blocks the HTTP response until all checks (including the LLM judge call)
    complete. Appropriate for pre-response-gate and inline-middleware use cases
    where the result must be known before the response is sent or streamed.
    """
    try:
        policy = get_policy(payload.use_case)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown use case: {payload.use_case}")

    if not payload.response.strip():
        raise HTTPException(status_code=400, detail="response must not be empty")

    result = await inspect(policy, payload.question, payload.context, payload.response)
    fix = _build_fix(payload, result)
    reasoning = _build_reasoning(result, policy)

    entry_id = storage.insert_log(
        storage.LogEntryIn(
            use_case=policy.label,
            question=payload.question,
            response=payload.response,
            responsibility_score=result.responsibility.score,
            performance_score=result.performance.score,
            cost_score=result.cost.score,
            total_score=result.total_score,
            decision=result.decision,
            reasoning=reasoning,
            latency_ms=result.latency_ms,
            over_budget=result.over_budget,
            override_reason=result.override_reason,
            compound_incident=result.compound_incident,
            incident_type=result.incident_type,
        )
    )

    return InspectResponse(
        id=entry_id,
        decision=result.decision,
        total_score=result.total_score,
        responsibility_score=result.responsibility.score,
        responsibility_flags=result.responsibility.flags,
        performance_score=result.performance.score,
        performance_reasoning=result.performance.reasoning,
        performance_method=result.performance.method,
        performance_confidence=result.performance.confidence,
        performance_no_context=result.performance.no_context,
        cost_score=result.cost.score,
        estimated_tokens=result.cost.estimated_tokens,
        budget_tokens=result.cost.budget_tokens,
        compound_incident=result.compound_incident,
        incident_type=result.incident_type,
        override_reason=result.override_reason,
        latency_ms=result.latency_ms,
        over_budget=result.over_budget,
        fix=fix,
    )


# ── Asynchronous inspection (decision — post-hoc audit) ───────────────────────

@app.post("/api/inspect-async", response_model=AsyncInspectResponse)
async def inspect_response_async(payload: InspectRequest, background_tasks: BackgroundTasks):
    """
    Post-hoc pipeline: return immediately with a queued ID, run the full
    inspection as a background task, and write the result to the audit log
    when complete. The caller is not blocked by the LLM judge call.

    This endpoint enforces the distinction between pipeline positions:
    a pre-response gate (chatbot) must block; a post-hoc audit (decision/loan)
    must not block the primary response path. Use GET /api/audit-log/{id} to
    poll for the completed result.

    Only configured for post-hoc use cases. Blocking/parallel use cases
    should use POST /api/inspect instead.
    """
    try:
        policy = get_policy(payload.use_case)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown use case: {payload.use_case}")

    if "post-hoc" not in policy.pipeline_position.lower():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Use case '{payload.use_case}' is configured as "
                f"'{policy.pipeline_position.split(' —')[0]}' — use POST /api/inspect "
                f"for blocking/parallel use cases. POST /api/inspect-async is only "
                f"appropriate for post-hoc audit pipelines."
            ),
        )

    if not payload.response.strip():
        raise HTTPException(status_code=400, detail="response must not be empty")

    # Pre-insert a PENDING row so the audit trail shows the queued inspection
    # immediately, even before the background task writes the real result.
    entry_id = storage.insert_pending(policy.label, payload.question, payload.response)

    background_tasks.add_task(_background_inspect, entry_id, policy, payload)

    return AsyncInspectResponse(
        queued_id=entry_id,
        pipeline_position=policy.pipeline_position,
        latency_budget_ms=policy.latency_budget_ms,
    )


async def _background_inspect(entry_id: int, policy, payload: InspectRequest) -> None:
    """
    Runs the full inspection pipeline and updates the pre-inserted PENDING
    audit log row with the completed result. Errors are swallowed rather than
    propagated (the caller has already received a 200) — in production this
    would write to a dead-letter queue or alert channel instead.
    """
    try:
        result = await inspect(policy, payload.question, payload.context, payload.response)
        fix = _build_fix(payload, result)  # noqa: F841 — stored in reasoning for now
        reasoning = _build_reasoning(result, policy)

        storage.update_log_entry(
            entry_id,
            storage.LogEntryIn(
                use_case=policy.label,
                question=payload.question,
                response=payload.response,
                responsibility_score=result.responsibility.score,
                performance_score=result.performance.score,
                cost_score=result.cost.score,
                total_score=result.total_score,
                decision=result.decision,
                reasoning=reasoning,
                latency_ms=result.latency_ms,
                over_budget=result.over_budget,
                override_reason=result.override_reason,
                compound_incident=result.compound_incident,
                incident_type=result.incident_type,
            ),
        )
    except Exception:
        # Don't crash the background task silently — mark the row as failed
        # so the audit log doesn't show a stale PENDING forever.
        storage.update_log_entry(
            entry_id,
            storage.LogEntryIn(
                use_case="",
                question=payload.question,
                response=payload.response,
                responsibility_score=0,
                performance_score=0,
                cost_score=0,
                total_score=0,
                decision="ERROR",
                reasoning="Background inspection failed — see server logs.",
            ),
        )


# ── Audit log ─────────────────────────────────────────────────────────────────

@app.get("/api/audit-log")
def get_audit_log(limit: int = 200):
    return storage.list_log(limit=limit)


@app.get("/api/audit-log/{entry_id}")
def get_audit_log_entry(entry_id: int):
    """Fetch a single audit log entry by ID — used by the frontend to poll
    for async inspection results."""
    entry = storage.get_log_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    return entry


@app.post("/api/audit-log/{entry_id}/review")
def review_entry(entry_id: int, payload: ReviewRequest):
    ok = storage.set_review(entry_id, payload.review)
    if not ok:
        raise HTTPException(status_code=404, detail="Log entry not found")
    return {"id": entry_id, "review": payload.review}


@app.delete("/api/audit-log")
def clear_audit_log():
    storage.clear_log()
    return {"cleared": True}


@app.get("/api/metrics")
def get_metrics():
    return storage.get_metrics()


# Serve the frontend as static files, mounted last so /api routes take priority.
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
