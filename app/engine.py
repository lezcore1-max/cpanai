"""
The inspection engine: runs all three checks (in parallel, to protect
latency for the blocking use cases), combines them into a single weighted
risk score per the active policy, and decides the routing action.

Compound-incident handling — philosophy: corroboration boost, not discount
────────────────────────────────────────────────────────────────────────────
When the responsibility check flags PII (or bias) *and* the performance check
independently judges the same response as likely hallucinated, two independent
detectors agree on the same underlying event. That agreement is evidence the
event is *real*, not a reason to shrink the score. We apply a 15% boost on
the combined responsibility+performance weighted contribution instead of
discounting it — because a fabricated SSN is worse than either a fabricated
fact or an SSN leak alone, and the two detectors corroborating each other
should push the case toward BLOCK faster, not slower.

Contrast with the anti-double-counting philosophy (would be a discount): that
would be defensible if the two checks shared a common noisy upstream signal and
we were worried about alert inflation. That's not the situation here — regex PII
detection and LLM-as-judge groundedness are fully independent detection paths,
so their agreement is signal, not noise.

Two safety guards on the compound path:
1. Performance confidence must be ≥ medium. A "low"-confidence, context-free
   plausibility guess cannot trigger a boost on a confirmed PII flag — that
   would let a shaky judge call amplify a slam-dunk PII leak's score in a way
   that's harder to explain to a reviewer.
2. The performance risk score threshold for "meaningful concern" is pulled from
   policy.thresholds["fix"], so stricter use cases (decision/loan, fix≥15) flag
   compound incidents at lower performance scores than lenient ones (chatbot,
   fix≥25). A hardcoded global threshold would be decoupled from the policy
   risk tolerance that drives everything else in the system.
"""

import asyncio
import time
from dataclasses import dataclass

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

# PII-type flag keywords — matched against flag label strings from responsibility.py
_PII_KEYWORDS = ("SSN", "Card", "Email", "Phone", "Address", "phone")

# Bias-type flag keywords — matched independently so a response can have both
# PII flags and bias flags simultaneously (e.g. "that neighborhood is higher risk,
# here is their SSN"). Previously `has_bias = not has_pii`, which made "mixed"
# a dead branch whenever PII was also present.
_BIAS_LABELS = (
    "Proxy-discrimination",
    "Group inference overriding",
    "Statistical group generalization",
    "Protected characteristic",
)


@dataclass
class InspectionResult:
    decision: str               # PASS | FIX | HUMAN | BLOCK
    total_score: int            # preserved as-is even when an override fires
    responsibility: ResponsibilityResult
    performance: PerformanceResult
    cost: CostResult
    compound_incident: bool     # True when PII/bias + hallucination corroborate each other
    incident_type: str          # "none" | "pii" | "bias" | "hallucination" | "cost" |
                                # "mixed" | "fabricated_pii" | "biased_hallucination"
    override_reason: str | None # Set when a hard-override changed the decision;
                                # None means the weighted score determined the outcome
    latency_ms: int             # wall-clock time of the concurrent checks (the gather)
    over_budget: bool           # True when latency_ms > policy.latency_budget_ms


def _apply_hard_overrides(
    responsibility: ResponsibilityResult,
    performance: PerformanceResult,
    cost: CostResult,
    current_decision: str,
    policy: UseCasePolicy,
) -> tuple[str, str | None]:
    """
    Runs after weighted scoring. Checks flags and performance risk against
    independently-disqualifying rules defined in responsibility.py and performance
    checks, overriding the decision if any are matched.

    Invariants enforced:
    1. No downgrade: a BLOCK can never be demoted to HUMAN by this function.
       All BLOCK-tier checks use early-returns before the HUMAN tier is
       reached; the HUMAN tier is additionally gated on current_decision not
       already being BLOCK or HUMAN.
    2. Deterministic precedence: BLOCK is always checked before HUMAN via
       code structure (early-returns), never by relying on set/dict iteration
       order, which is not stable across refactors.
    3. Doubly-confirmed audit trail: override_reason is populated even when
       current_decision was already BLOCK from scoring — so the audit log
       distinguishes "score crossed threshold" from "score AND hard rule both
       fired," which is a stronger signal for downstream weight tuning.

    The total_score is intentionally left unchanged — the override only
    affects the decision field so reviewers can see what the weighted model
    thought alongside why the override fired.

    Returns (decision, override_reason). override_reason is None only when
    no override fired — meaning the weighted score alone determined the outcome.
    """

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
    # Intrinsic safety violations trigger BLOCK regardless of whether source context was provided,
    # but require at least medium confidence to prevent low-confidence heuristic fallbacks from forcing BLOCK.
    if (
        performance 
        and performance.safety_concern 
        and performance.score >= 60
        and performance.confidence in ("high", "medium")
    ):
        return "BLOCK", _reason(
            "BLOCK",
            f"unauthorized account access / credential fabrication (safety concern flagged) — '{performance.reasoning}'",
        )

    # Extreme ungroundedness override (requires high or medium confidence).
    if performance and performance.confidence in ("high", "medium") and performance.score >= 90:
        return "BLOCK", _reason(
            "BLOCK",
            f"extreme ungroundedness/hallucination (performance risk {performance.score}/100) — '{performance.reasoning}'",
        )

    # ── Tier 2: HUMAN-escalation patterns ────────────────────────────────────
    # Gated on current_decision not already being BLOCK or HUMAN — this is the
    # explicit no-downgrade guard. A score-based BLOCK is never demoted here.
    if current_decision not in ("BLOCK", "HUMAN"):
        for flag in responsibility.flags:
            if any(auto_flag in flag for auto_flag in AUTO_HUMAN_FLAGS):
                return "HUMAN", _reason(
                    "HUMAN",
                    f"noisy PII pattern — verify manually before routing — '{flag}'",
                )

        if performance and performance.confidence in ("high", "medium") and performance.score >= 70:
            return "HUMAN", _reason(
                "HUMAN",
                f"high performance risk ({performance.score}/100) — '{performance.reasoning}'",
            )

    # ── Tier 3: FIX-escalation patterns ──────────────────────────────────────
    # Ensures severe cost overruns alone trigger cost trimming even if weighted total was slightly below FIX threshold.
    if current_decision == "PASS":
        if cost and cost.score >= 90:
            return "FIX", _reason(
                "FIX",
                f"severe cost budget overage (~{cost.estimated_tokens} est. tokens vs {cost.budget_tokens} budget) — auto-trimmed",
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


async def inspect_payload(
    arg1,
    arg2,
    arg3,
    arg4,
) -> InspectionResult:
    """
    Main entrypoint for inspecting a single AI response against policy.
    Flexible signature accepts both (policy, question, context, response)
    and (question, context, response, policy).
    """
    if isinstance(arg1, UseCasePolicy):
        policy = arg1
        question = str(arg2 or "")
        context = str(arg3 or "")
        response = str(arg4 or "")
    else:
        question = str(arg1 or "")
        context = str(arg2 or "")
        response = str(arg3 or "")
        policy = arg4

    start_time = time.perf_counter()

    # Execute all 3 checks concurrently
    responsibility, performance, cost = await asyncio.gather(
        asyncio.to_thread(check_responsibility, response),
        check_performance(question, context, response),
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
    # Check if responsibility (PII/bias) and performance (hallucination) agree
    has_resp_concern = len(responsibility.flags) > 0
    has_perf_concern = (
        performance.confidence in ("high", "medium")
        and performance.score >= policy.thresholds["fix"]
    )
    compound_incident = has_resp_concern and has_perf_concern

    final_weighted = raw_total
    if compound_incident:
        # Apply 15% corroboration boost on combined responsibility + performance contribution
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

    # Apply hard rule overrides
    final_decision, override_reason = _apply_hard_overrides(
        responsibility, performance, cost, base_decision, policy
    )

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
    )

# Alias for backwards compatibility
inspect = inspect_payload
