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
    # Intrinsic safety violations trigger BLOCK regardless of whether source context was provided.
    if performance and performance.safety_concern and performance.score >= 60:
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



def _classify_incident(
    responsibility: ResponsibilityResult,
    performance: PerformanceResult,
    cost: CostResult,
    policy: UseCasePolicy,
) -> tuple[bool, str]:
    """
    Returns (compound_incident, incident_type).

    has_pii and has_bias are now classified independently from flag label text,
    so both can be True simultaneously (e.g. a response that leaks an SSN and
    also uses a proxy-discrimination phrase).

    The hallucination threshold uses policy.thresholds["fix"] so that stricter
    use cases (loan: fix≥15) register compound incidents at lower performance
    scores than lenient ones (chatbot: fix≥25).

    Low-confidence performance scores are excluded from compound detection to
    prevent a shaky, context-free plausibility guess from amplifying a confirmed
    PII flag's route.
    """
    flags = responsibility.flags

    has_pii = any(
        any(kw in flag for kw in _PII_KEYWORDS)
        for flag in flags
    )
    has_bias = any(
        any(lbl.lower() in flag.lower() for lbl in _BIAS_LABELS)
        for flag in flags
    )

    # Use policy-specific FIX threshold as the "meaningful concern" floor for
    # performance, and exclude low-confidence scores (forced when no context).
    perf_threshold = policy.thresholds["fix"]
    has_hallucination = (
        performance.score >= perf_threshold
        and performance.confidence != "low"
    )

    has_cost = cost.score > 0

    # Compound = at least one responsibility signal + hallucination
    compound = (has_pii or has_bias) and has_hallucination

    if has_pii and has_bias and has_hallucination:
        incident_type = "fabricated_pii"    # most severe: PII + bias + hallucination
    elif has_pii and has_hallucination:
        incident_type = "fabricated_pii"
    elif has_bias and has_hallucination:
        incident_type = "biased_hallucination"
    elif has_pii and has_bias:
        incident_type = "mixed"             # same check, two sub-types — no cross-check compound
    elif has_pii:
        incident_type = "pii"
    elif has_bias:
        incident_type = "bias"
    elif has_hallucination:
        incident_type = "hallucination"
    elif has_cost:
        incident_type = "cost"
    else:
        incident_type = "none"

    return compound, incident_type


async def inspect(
    policy: UseCasePolicy, question: str, context: str, response: str
) -> InspectionResult:
    # Responsibility and cost are cheap/sync; performance needs a network call.
    # Running them concurrently means wall-clock latency ≈ slowest check (the
    # LLM call), not the sum of all three.
    responsibility_task = asyncio.to_thread(check_responsibility, response + " " + question)
    cost_task = asyncio.to_thread(check_cost, response, policy.cost_budget_tokens)
    performance_task = check_performance(question, context, response)

    # Timer covers only the concurrent checks — that is the latency the policy
    # budget is actually describing (not scoring/routing arithmetic above).
    t0 = time.perf_counter()
    responsibility, cost, performance = await asyncio.gather(
        responsibility_task, cost_task, performance_task
    )
    latency_ms = round((time.perf_counter() - t0) * 1000)
    over_budget = latency_ms > policy.latency_budget_ms

    compound_incident, incident_type = _classify_incident(
        responsibility, performance, cost, policy
    )

    resp_weighted = responsibility.score * policy.weights["responsibility"]
    perf_weighted = performance.score * policy.weights["performance"]
    cost_weighted = cost.score * policy.weights["cost"]

    if compound_incident:
        # Two independent detectors agreeing on the same underlying event is
        # evidence of confidence, not a reason to shrink the score.
        # Apply a 15% corroboration boost on the combined resp+perf contribution.
        #
        # Concrete check (chatbot policy, weights resp=0.5 perf=0.3, block≥65):
        #   resp=100 (SSN+card+email), perf=90 (hallucinated):
        #   overlap = 50 + 27 = 77 → boosted = 77 × 1.15 = 88.5 → total ≈ 89 → BLOCK ✓
        #   naive sum would be 77 → also BLOCK, but now compound is *at least* as
        #   severe, never softer than treating them independently.
        overlap = resp_weighted + perf_weighted
        total_score = round(min(100, overlap * 1.15 + cost_weighted))
    else:
        total_score = round(resp_weighted + perf_weighted + cost_weighted)

    thresholds = policy.thresholds
    if total_score >= thresholds["block"]:
        decision = "BLOCK"
    elif total_score >= thresholds["human"]:
        decision = "HUMAN"
    elif total_score >= thresholds["fix"]:
        decision = "FIX"
    else:
        decision = "PASS"

    # Hard-override layer: runs after scoring, changes decision only.
    # total_score is preserved so the audit log remains meaningful.
    decision, override_reason = _apply_hard_overrides(responsibility, performance, cost, decision, policy)

    return InspectionResult(
        decision=decision,
        total_score=total_score,
        responsibility=responsibility,
        performance=performance,
        cost=cost,
        compound_incident=compound_incident,
        incident_type=incident_type,
        override_reason=override_reason,
        latency_ms=latency_ms,
        over_budget=over_budget,
    )
