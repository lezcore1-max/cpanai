"""
Auto-correction for FIX-routed responses. A FIX decision means the risk is
real but minor enough to correct automatically rather than block or escalate
— this module is what actually performs that correction, so "FIX" is a real
action rather than a label.
"""

import re
from dataclasses import dataclass

from app.checks.cost import CostResult
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
    response: str, responsibility: ResponsibilityResult, cost: CostResult
) -> FixResult | None:
    # Whichever check is driving the risk gets the matching correction.
    if responsibility.score >= cost.score and responsibility.score > 0:
        fixed = response
        for pattern, tag in _REDACTIONS:
            fixed = pattern.sub(tag, fixed)
        return FixResult(method="PII redaction", before=response, after=fixed)

    if cost.score > 0:
        words = response.split()
        target_words = max(15, round(len(words) / (1 + cost.score / 100)))
        trimmed = " ".join(words[:target_words])
        trimmed += " … (trimmed by ControlPlane — response exceeded cost budget for this use case)"
        return FixResult(method="Cost trim", before=response, after=trimmed)

    return None
