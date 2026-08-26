from pydantic import BaseModel


class InspectRequest(BaseModel):
    use_case: str
    question: str = ""
    context: str = ""
    response: str


class FixOut(BaseModel):
    method: str
    before: str
    after: str


class InspectResponse(BaseModel):
    id: int
    decision: str
    total_score: int
    responsibility_score: int
    responsibility_flags: list[str]
    performance_score: int
    performance_reasoning: str
    performance_method: str
    performance_confidence: str     # "high" | "medium" | "low"
    performance_no_context: bool    # True when no source context was provided
    cost_score: int
    estimated_tokens: int
    budget_tokens: int
    compound_incident: bool         # True when PII + hallucination fired together
    incident_type: str
    override_reason: str | None     # Set when a hard-override changed the decision
    latency_ms: int                 # wall-clock time of the concurrent checks
    over_budget: bool               # True when latency_ms > policy.latency_budget_ms
    fix: FixOut | None = None


class AsyncInspectResponse(BaseModel):
    """Returned immediately by POST /api/inspect-async for post-hoc use cases."""
    queued_id: int
    status: str = "pending"
    pipeline_position: str
    latency_budget_ms: int


class ReviewRequest(BaseModel):
    review: str | None  # "confirm" | "override" | None
