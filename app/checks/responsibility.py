"""
Responsibility check — PII leakage and bias/fairness pattern detection.

Deterministic and fast (regex-based), so it never adds meaningful latency,
which matters for the pre-response-gate use cases where every millisecond
counts. Trades recall for speed and explainability: every flag raised here
can be pointed to a specific matched pattern, which matters for audit trails.

Known limitations (by design):
- PII split across sentence boundaries is not detected; a production system
  would run a sliding-window or NER model over the full conversation turn.
- Non-Western name/address formats are not covered beyond international phone.
- The 9-digit SSN run pattern is intentionally noisier (lower weight) and
  will fire on long numeric IDs; it's a signal, not a verdict.
- Bias detection is illustrative — a production system would add an
  embedding-based fairness classifier over the full response.
"""

import re
from dataclasses import dataclass, field


@dataclass
class ResponsibilityResult:
    score: int
    flags: list[str] = field(default_factory=list)


_PATTERNS = [
    # ── PII ─────────────────────────────────────────────────────────────────
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
     "SSN pattern (dashed)", 45),
    (re.compile(r"\b\d{3}\s\d{2}\s\d{4}\b"),
     "SSN pattern (spaced)", 45),
    # Lower weight — 9-digit run is noisier but catches the no-separator form
    (re.compile(r"(?<!\d)\d{9}(?!\d)"),
     "Possible SSN (9-digit run — verify manually)", 25),
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
     "Card number (spaced/dashed)", 45),
    # Catches 16-digit runs with no separator (Visa/MC/Discover prefix heuristic)
    (re.compile(r"(?<!\d)[4-6]\d{15}(?!\d)"),
     "Possible card number (16-digit run)", 35),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I),
     "Email address", 25),
    (re.compile(r"\b\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
     "Phone number (North American format)", 20),
    (re.compile(r"\+\d{1,3}[\s.-]?\d{4,14}(?!\d)"),
     "International phone number", 20),
    (re.compile(
        r"\b\d{1,5}\s\w+\s(st|street|ave|avenue|rd|road|blvd|lane|ln|dr|drive|ct|court|pl|place|way)\b",
        re.I),
     "Street address (Western format)", 20),

    # ── Bias / fairness ──────────────────────────────────────────────────────
    (
        re.compile(
            r"\b(zip code|neighborhood|those people|that demographic|that area|"
            r"that community|that group|that population)\b.{0,80}"
            r"\b(risk|dangerous|less likely|lower limit|higher rate|deny|decline|"
            r"suspicious|fraudulent)\b",
            re.I,
        ),
        "Proxy-discrimination pattern (group characteristic used as individual risk proxy)",
        55,
    ),
    (
        re.compile(
            r"\b(regardless of|instead of|despite|ignoring).{0,60}"
            r"(individual|their own|specific|personal|actual|verified)\b",
            re.I,
        ),
        "Group inference overriding individual assessment",
        40,
    ),
    (
        re.compile(
            r"\b(statistically|historically|typically|tend to be|are more likely|are known to)"
            r".{0,80}\b(risk|default|fail|crime|criminal|dangerous|problematic|unreliable)\b",
            re.I,
        ),
        "Statistical group generalization applied to individual decision",
        40,
    ),
    (
        re.compile(
            r"\b(race|ethnicity|gender|religion|national origin|nationality|age|disability|"
            r"marital status).{0,80}\b(risk|deny|decline|higher|lower|likely|approve|reject)\b",
            re.I,
        ),
        "Protected characteristic linked to risk or outcome",
        60,
    ),
]

# ── Hard-override flag sets (consumed by engine.py) ─────────────────────────
# Deliberately conservative: only patterns whose false-positive rate is low
# enough that forcing a hard BLOCK will produce fewer wrong outcomes than
# letting them through the weighted-score path.
#
# NOT included: phone numbers, email addresses, street addresses, 9-digit SSN
# runs — all of those are too noisy to treat as automatically disqualifying.

# Any of these → force BLOCK regardless of weighted total_score.
AUTO_BLOCK_FLAGS: frozenset[str] = frozenset({
    "SSN pattern (dashed)",         # \d{3}-\d{2}-\d{4}  — very low FP risk
    "SSN pattern (spaced)",         # \d{3} \d{2} \d{4}  — very low FP risk
    "Card number (spaced/dashed)",  # 16-digit formatted  — very low FP risk
})

# Any of these → force at-minimum HUMAN (not BLOCK — the pattern is noisier;
# a false BLOCK harms more than routing a human to verify).
AUTO_HUMAN_FLAGS: frozenset[str] = frozenset({
    "Possible SSN (9-digit run — verify manually)",
})

# Regulated-context-only overrides: independently disqualifying only when the
# active use case is "decision" (credit/loan) because a protected-characteristic-
# linked-to-outcome finding is a clear ECOA/FHA violation in that context.
# In chatbot/copilot the same flag routes normally through the weighted score.
AUTO_BLOCK_FLAGS_DECISION: frozenset[str] = frozenset({
    "Protected characteristic linked to risk or outcome",
})


def check_responsibility(text: str) -> ResponsibilityResult:
    flags: list[str] = []
    score = 0
    for pattern, label, weight in _PATTERNS:
        if pattern.search(text):
            flags.append(label)
            score += weight
    return ResponsibilityResult(score=min(100, score), flags=flags)
