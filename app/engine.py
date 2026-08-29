import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.checks.cost import CostResult, check_cost
from app.checks.performance import PerformanceResult, check_performance
from app.checks.responsibility import (
    AUTO_BLOCK_FLAGS,
    AUTO_BLOCK_FLAGS_DECISION,
    AUTO_HUMAN_FLAGS,
    ResponsibilityResult,
    check_responsibility,
)
from app.config import UseCasePolicy

# Keywords/labels used for audit-trail incident classification
_PII_KEYWORDS = ("ssn", "card number", "email", "phone", "street address", "credential")
_BIAS_LABELS = ("protected characteristic", "proxy-discrimination")


@dataclass
class InspectionResult:
    decision: str  # "PASS" | "FIX" | "HUMAN" | "BLOCK"
    total_score: int  # 0 to 100
    responsibility: ResponsibilityResult
    performance: PerformanceResult
    cost: CostResult
    compound_incident: bool
    incident_type: str  # "none" | "pii" | "bias" | "hallucination" | "cost" | "fabricated_pii" | "biased_hallucination" | "mixed"
    override_reason: str | None = None
    latency_ms: int = 0
    over_budget: bool = False
    session_id: str | None = None
    session_cumulative_risk: float | None = None
    session_escalation_streak: int | None = None
    is_action: bool = False
    action_reversible: bool = True


def _apply_hard_overrides(
    responsibility: ResponsibilityResult,
    performance: PerformanceResult,
    cost: CostResult,
    current_decision: str,
    policy: UseCasePolicy,
    session: Any | None = None,
    is_action: bool = False,
    action_reversible: bool = True,
) -> tuple[str, str | None]:
    override_reason: str | None = None

    def _reason(target: str, detail: str) -> str:
        """
        Produces a human-readable override reason that distinguishes two cases:
        - The override *changed* the decision (elevated current_decision → target).
        - The override *confirmed* a decision the weighted score already made
          (both paths agree, recorded for audit completeness).
        """
        if current_decision == target:
            return (
                f"hard-override confirmed "
                f"(weighted score independently reached {target}): {detail}"
            )
        return f"hard-override elevated {current_decision} → {target}: {detail}"

    # ── Tier 1: BLOCK-forcing patterns ───────────────────────────────────────
    # Checked first via early-returns so a BLOCK match always takes precedence
    # over the HUMAN tier below, regardless of which flags appear in the list.

    for flag in responsibility.flags:
        if any(auto_flag in flag for auto_flag in AUTO_BLOCK_FLAGS):
            return "BLOCK", _reason("BLOCK", f"high-confidence PII match — '{flag}'")

    # Protected characteristic linked to outcome is independently disqualifying
    # only when policy.key == "decision" (credit/loan underwriting).
    if policy.key == "decision":
        for flag in responsibility.flags:
            if any(auto_flag in flag for auto_flag in AUTO_BLOCK_FLAGS_DECISION):
                return "BLOCK", _reason(
                    "BLOCK",
                    f"protected-characteristic flag in regulated decision context "
                    f"(ECOA/FHA) — '{flag}'",
                )

    # Structured safety concern (unauthorized account access / takeover, credential / password revelation).
    if (
        performance 
        and performance.safety_concern 
        and performance.score >= policy.thresholds.get("block", 60)
        and performance.confidence in ("high", "medium")
    ):
        return "BLOCK", _reason(
            "BLOCK",
            f"unauthorized account access / credential fabrication (safety concern flagged) — '{performance.reasoning}'",
        )

    # Extreme ungroundedness override (requires grounded context contradiction or high-confidence wild claim).
    if performance and not performance.no_context and performance.confidence in ("high", "medium") and performance.score >= 90:
        return "BLOCK", _reason(
            "BLOCK",
            f"extreme ungroundedness/hallucination (performance risk {performance.score}/100) — '{performance.reasoning}'",
        )

    # Semantic bias detection in regulated decision contexts (ECOA / FHA compliance).
    if (
        performance
        and performance.bias_detected
        and use_case == "decision"
        and performance.confidence in ("high", "medium")
    ):
        return "BLOCK", _reason(
            "BLOCK",
            f"semantic bias / proxy discrimination detected in regulated decision context (ECOA/FHA) — '{performance.reasoning}'",
        )

    # ── Tier 2: HUMAN-escalation patterns ────────────────────────────────────
    # Gated on current_decision not already being BLOCK or HUMAN — this is the
    # explicit no-downgrade guard. A score-based BLOCK is never demoted here.
    if current_decision not in ("BLOCK", "HUMAN"):
        for flag in responsibility.flags:
            if any(auto_flag.lower() in flag.lower() for auto_flag in AUTO_HUMAN_FLAGS):
                return "HUMAN", _reason(
                    "HUMAN",
                    f"noisy PII or bias pattern — verify manually before routing — '{flag}'",
                )

        # Semantic bias detection in general / unregulated contexts (escalates to HUMAN).
        if (
            performance
            and performance.bias_detected
            and performance.confidence in ("high", "medium")
        ):
            return "HUMAN", _reason(
                "HUMAN",
                f"semantic group bias / proxy-discrimination detected — '{performance.reasoning}'",
            )

        if performance and performance.confidence in ("high", "medium") and performance.score >= 70:
            return "HUMAN", _reason(
                "HUMAN",
                f"high performance risk ({performance.score}/100) — '{performance.reasoning}'",
            )

    # ── Tier 4: Session Compounding Risk & Agentic Action Overrides ──────────
    # Placed above Tier 3 FIX so session escalation streaks and irreversible actions
    # properly override FIX decisions to HUMAN or BLOCK.
    if is_action and not action_reversible and getattr(policy, "require_human_for_irreversible_actions", True):
        if current_decision not in ("BLOCK", "HUMAN"):
            current_decision, override_reason = "HUMAN", _reason(
                "HUMAN",
                "irreversible agentic action requires mandatory human review",
            )

    if session:
        streak = getattr(session, "escalation_streak", 0)
        cum_risk = getattr(session, "cumulative_risk", 0.0)
        block_thresh = policy.thresholds.get("block", 60)

        if streak >= 3 and current_decision not in ("BLOCK", "HUMAN"):
            current_decision, override_reason = "HUMAN", _reason(
                "HUMAN",
                f"session escalation pattern: {streak} consecutive flagged turns",
            )
        if cum_risk >= block_thresh * 1.5 and current_decision != "BLOCK":
            return "BLOCK", _reason(
                "BLOCK",
                f"cumulative session risk ({cum_risk:.0f}) exceeds sustained-pattern threshold ({block_thresh * 1.5:.0f})",
            )

    # If session/action override escalated decision to HUMAN or BLOCK, return it early
    if override_reason is not None:
        return current_decision, override_reason

    # ── Tier 3: FIX-escalation patterns ──────────────────────────────────────
    # Auto-redactable PII (email / phone) and severe cost overruns trigger FIX auto-remediation.
    if current_decision == "PASS":
        for flag in responsibility.flags:
            if any(kw in flag for kw in ("Email address", "Phone number")):
                return "FIX", _reason(
                    "FIX",
                    f"auto-redactable PII match — '{flag}'",
                )

    return current_decision, None


def _classify_incident_type(
    has_pii: bool,
    has_bias: bool,
    has_hallucination: bool,
    has_cost: bool,
    safety_concern: bool = False,
) -> str:
    """
    Classifies the dominant incident pattern for telemetry and audit log reporting.

    Precedence:
    1. Mixed / Compound combinations (fabricated PII, biased hallucination, mixed PII+bias)
    2. Single primary flags (PII, bias, hallucination, cost)
    3. "none" if no detectors fired
    """
    if (has_pii or safety_concern) and has_hallucination:
        return "fabricated_pii"
    if has_bias and has_hallucination:
        return "biased_hallucination"
    if has_pii and has_bias:
        return "mixed"
    if has_pii or safety_concern:
        return "pii"
    if has_bias:
        return "bias"
    if has_hallucination:
        return "hallucination"
    if has_cost:
        return "cost"
    return "none"


async def inspect_payload(*args, **kwargs) -> InspectionResult:
    """
    Runs responsibility, performance (groundedness), and cost checks concurrently
    and combines their scores and hard rules into a unified InspectionResult.
    """
    start_time = time.perf_counter()

    # Flexible positional argument handling
    policy = kwargs.get("policy")
    question = kwargs.get("question", "")
    context = kwargs.get("context", "")
    response = kwargs.get("response", "")
    session_id = kwargs.get("session_id")
    is_action = kwargs.get("is_action", False)
    action_reversible = kwargs.get("action_reversible", True)

    pos_args = list(args)
    if not policy and pos_args:
        for arg in list(pos_args):
            if isinstance(arg, UseCasePolicy):
                policy = arg
                pos_args.remove(arg)
                break

    if pos_args:
        if len(pos_args) >= 3:
            question, context, response = pos_args[0], pos_args[1], pos_args[2]
        elif len(pos_args) == 1:
            response = pos_args[0]

    if not policy:
        from app.config import get_policy
        policy = get_policy("chatbot")

    # Fetch/initialize session state if session_id is provided
    session = None
    if session_id:
        from app import storage
        session = storage.get_session(session_id)
        if not session:
            session = storage.SessionState(session_id=session_id, use_case=policy.key)

    # Run checks in parallel via asyncio.gather
    responsibility, performance, cost = await asyncio.gather(
        asyncio.to_thread(check_responsibility, response, policy),
        check_performance(question, context, response, policy),
        asyncio.to_thread(check_cost, response, policy),
    )

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    over_budget = elapsed_ms > policy.latency_budget_ms

    # Calculate base weighted risk score
    raw_total = (
        responsibility.score * policy.weights["responsibility"]
        + performance.score * policy.weights["performance"]
        + cost.score * policy.weights["cost"]
    )

    # Compound incident detection (corroboration boost)
    has_resp_concern = len(responsibility.flags) > 0
    has_perf_concern = (
        performance.confidence in ("high", "medium")
        and performance.score >= policy.thresholds["fix"]
    )
    compound_incident = has_resp_concern and has_perf_concern

    final_weighted = raw_total
    if compound_incident:
        resp_perf_contrib = (
            responsibility.score * policy.weights["responsibility"]
            + performance.score * policy.weights["performance"]
        )
        boost = resp_perf_contrib * 0.15
        final_weighted = min(100.0, raw_total + boost)

    total_score = int(round(final_weighted))

    # Base decision from weighted total risk score
    if total_score >= policy.thresholds["block"]:
        base_decision = "BLOCK"
    elif total_score >= policy.thresholds["human"]:
        base_decision = "HUMAN"
    elif total_score >= policy.thresholds["fix"]:
        base_decision = "FIX"
    else:
        base_decision = "PASS"

    # Apply hard rule overrides (including session compounding risk & agentic action gates)
    final_decision, override_reason = _apply_hard_overrides(
        responsibility,
        performance,
        cost,
        base_decision,
        policy,
        session=session,
        is_action=is_action,
        action_reversible=action_reversible,
    )

    # Update session state and persist if active session
    if session:
        from app import storage
        session = storage.update_session_state(session, total_score, final_decision)
        storage.save_session(session)

    # Classify incident type for audit reporting
    has_pii = any(any(kw.lower() in f.lower() for kw in _PII_KEYWORDS) for f in responsibility.flags)
    has_bias = any(any(lbl.lower() in f.lower() for lbl in _BIAS_LABELS) for f in responsibility.flags)
    has_hallucination = performance.score >= policy.thresholds["fix"]
    has_cost = cost.score >= policy.thresholds["fix"]

    incident_type = _classify_incident_type(
        has_pii, has_bias, has_hallucination, has_cost, safety_concern=performance.safety_concern
    )

    return InspectionResult(
        decision=final_decision,
        total_score=total_score,
        responsibility=responsibility,
        performance=performance,
        cost=cost,
        compound_incident=compound_incident,
        incident_type=incident_type,
        override_reason=override_reason,
        latency_ms=elapsed_ms,
        over_budget=over_budget,
        session_id=session_id,
        session_cumulative_risk=session.cumulative_risk if session else None,
        session_escalation_streak=session.escalation_streak if session else None,
        is_action=is_action,
        action_reversible=action_reversible,
    )

# Alias for backwards compatibility
inspect = inspect_payload
