"""
Auto-correction for FIX-routed responses. A FIX decision means the risk is
real but minor enough to correct automatically rather than block or escalate
— this module is what actually performs that correction, so "FIX" is a real
action rather than a label.

Handles all three risk dimensions:
1. Responsibility driving risk -> PII redaction
2. Cost driving risk -> Word-count trim with truncation notice
3. Performance driving risk -> Appends explicit verification caveat disclaimer
"""

import re
from dataclasses import dataclass

from app.checks.cost import CostResult
from app.checks.performance import PerformanceResult
from app.checks.responsibility import ResponsibilityResult

_REDACTIONS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN REDACTED]"),
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CARD REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I), "[EMAIL REDACTED]"),
    (re.compile(r"\b\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "[PHONE REDACTED]"),
    (re.compile(r"\b\d{1,5}\s\w+\s(st|street|ave|avenue|rd|road|blvd)\b", re.I), "[ADDRESS REDACTED]"),
]


@dataclass
class FixResult:
    method: str
    before: str
    after: str


def apply_fix(
    response: str,
    responsibility: ResponsibilityResult,
    cost: CostResult,
    performance: PerformanceResult | None = None,
) -> FixResult | None:
    resp_score = responsibility.score
    cost_score = cost.score
    perf_score = performance.score if performance else 0

    # Whichever check is driving the highest risk gets the matching correction.
    if resp_score >= cost_score and resp_score >= perf_score and resp_score > 0:
        fixed = response
        for pattern, tag in _REDACTIONS:
            fixed = pattern.sub(tag, fixed)
        return FixResult(method="PII redaction", before=response, after=fixed)

    if perf_score >= resp_score and perf_score >= cost_score and perf_score > 0:
        caveat = (
            " \n\n[ControlPlane Notice: Automatically appended verification caveat "
            "— response flagged as ungrounded or potentially unverified claim.]"
        )
        return FixResult(method="Hallucination disclaimer", before=response, after=response + caveat)

    if cost_score > 0:
        words = response.split()
        target_words = max(15, round(len(words) / (1 + cost_score / 100)))
        trimmed = " ".join(words[:target_words])
        trimmed += " … (trimmed by ControlPlane — response exceeded cost budget for this use case)"
        return FixResult(method="Cost trim", before=response, after=trimmed)

    return None
