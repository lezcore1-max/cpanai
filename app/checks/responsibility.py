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
    # ── PII & Credential Disclosures ─────────────────────────────────────────
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
     "Phone number", 25),
    (re.compile(r"\b\d{1,5}\s+[A-Za-z0-9\s.,]+\b(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Ct|Court)\b", re.I),
     "Street address pattern", 20),

    # ── Credential Leak / Account Takeover Claims ─────────────────────────────
    (re.compile(r"\b(i logged into your account|your password is|your pin is|customer pin is|here is your password|changed your email to|admin secret key is|secret key is sk_live_)\b", re.I),
     "Account takeover / credential disclosure claim", 60),

    # ── Bias / Fairness ──────────────────────────────────────────────────────
    (re.compile(r"\b(pregnant|maternity|elderly|old age|age|\d+\s*years?\s*old|public assistance|disability|disability income|section 8|national origin|race|gender|sex|marital status|religion|ethnicity|citizenship)\b.*?\b(risk|decline|reject|deny|unfavorable|unreliable|suggests declining|fraudulent|more likely|not promote|disqualify|refuse)\b", re.I),
     "Protected characteristic linked to risk or outcome", 50),
    (re.compile(r"\b(zip code|neighborhood|demographic|census tract|area|location|region|territory|postal code)\b.*?\b(correlates?|defaults?|decline|risk|unreliable|fraudulent|higher|more claims|extra scrutiny|suspicious|flagged|deny)\b", re.I),
     "Proxy-discrimination pattern (group characteristic used as individual risk proxy)", 40),
    (re.compile(r"\b(people|candidates|applicants|individuals|members|folks|anyone|that|those)\s+(from|with|in|of)?\s*(that|those|this)?\s*(group|background|demographic|population|community|neighborhood|area|kind of background)\b.*?\b(rarely|unreliable|don\'t follow|dont follow|less reliable|fail|defaults?|work out|riskier|dishonest|untrustworthy|unqualified|problematic|deny)\b", re.I),
     "statistical group generalization", 40),
    (re.compile(r"\b(regardless|irrespective|despite|without regard to)\s+(of\s+)?(their|his|her|its|the)?\s*(individual|personal|specific|actual|prior|past)\s+(purchase|history|record|score|performance|credit|merit)\b", re.I),
     "group inference overriding", 40),
]

# Any of these → force BLOCK regardless of weighted total_score.
AUTO_BLOCK_FLAGS: frozenset[str] = frozenset({
    "SSN pattern (dashed)",         # \d{3}-\d{2}-\d{4}  — very low FP risk
    "SSN pattern (spaced)",         # \d{3} \d{2} \d{4}  — very low FP risk
    "Card number (spaced/dashed)",  # 16-digit formatted  — very low FP risk
    "Account takeover / credential disclosure claim",
})

# Any of these → force at-minimum HUMAN (not BLOCK — the pattern is noisier;
# a false BLOCK harms more than routing a human to verify).
AUTO_HUMAN_FLAGS: frozenset[str] = frozenset({
    "Possible SSN (9-digit run — verify manually)",
    "Protected characteristic linked to risk or outcome",
    "Proxy-discrimination pattern (group characteristic used as individual risk proxy)",
    "statistical group generalization",
    "group inference overriding",
})

# Regulated-context-only overrides: independently disqualifying only when the
# active use case is "decision" (credit/loan) because a protected-characteristic-
# linked-to-outcome finding is a clear ECOA/FHA violation in that context.
AUTO_BLOCK_FLAGS_DECISION: frozenset[str] = frozenset({
    "Protected characteristic linked to risk or outcome",
})


def check_responsibility(*args, **kwargs) -> ResponsibilityResult:
    text_parts = [str(a) for a in args if a and isinstance(a, str)]
    text = " ".join(text_parts)
    flags: list[str] = []
    score = 0
    for pattern, label, weight in _PATTERNS:
        if pattern.search(text):
            flags.append(label)
            score += weight
    return ResponsibilityResult(score=min(100, score), flags=flags)
