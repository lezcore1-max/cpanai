"""
Policy configuration for ControlPlane.

Each use case gets its own policy: how much each risk dimension is weighted,
what score triggers what action, how many tokens it's allowed to spend, and
where in the pipeline the checker sits. This is what lets the same flagged
response get BLOCKED in one use case and only FLAGGED in another.

In a real deployment this would live in a database / config service and be
editable per team, geography, or regulatory regime without a code change.
"""

from pydantic import BaseModel


class UseCasePolicy(BaseModel):
    key: str
    label: str
    description: str
    pipeline_position: str          # where the checker sits for this use case
    latency_budget_ms: int          # illustrative — informs blocking vs async design
    weights: dict                   # responsibility / performance / cost -> 0..1, sums to 1
    thresholds: dict                # block / human / fix -> risk score 0..100
    cost_budget_tokens: int         # expected token budget for a "normal" response


USE_CASES: dict[str, UseCasePolicy] = {
    "chatbot": UseCasePolicy(
        key="chatbot",
        label="Customer Chatbot",
        description="External, real-time customer support assistant.",
        pipeline_position="Pre-response gate (blocking) — checked before the answer reaches the customer.",
        latency_budget_ms=800,
        weights={"responsibility": 0.5, "performance": 0.3, "cost": 0.2},
        thresholds={"block": 65, "human": 45, "fix": 25},
        cost_budget_tokens=220,
    ),
    "copilot": UseCasePolicy(
        key="copilot",
        label="Internal Copilot",
        description="Employee-facing knowledge assistant, multi-turn.",
        pipeline_position="Inline middleware (parallel, non-blocking) — checks run alongside response streaming.",
        latency_budget_ms=3000,
        weights={"performance": 0.5, "responsibility": 0.3, "cost": 0.2},
        thresholds={"block": 80, "human": 60, "fix": 35},
        cost_budget_tokens=700,
    ),
    "decision": UseCasePolicy(
        key="decision",
        label="Decision-Support (loan)",
        description="Regulated decision-support tool for credit/loan pre-approval.",
        pipeline_position="Post-hoc audit + mandatory human sign-off above the human threshold.",
        latency_budget_ms=10000,
        weights={"responsibility": 0.45, "performance": 0.35, "cost": 0.2},
        thresholds={"block": 50, "human": 30, "fix": 15},
        cost_budget_tokens=400,
    ),
}


def get_policy(use_case: str) -> UseCasePolicy:
    if use_case not in USE_CASES:
        raise KeyError(f"Unknown use case: {use_case}")
    return USE_CASES[use_case]
