"""
Cost check — flags silent waste: unnecessary length, verbosity, or compute
relative to what the use case actually needs. This is the dimension most
"AI safety" checkers skip, but it's a real line item at tens of thousands
of interactions per week.
"""

import math
from dataclasses import dataclass


@dataclass
class CostResult:
    score: int
    estimated_tokens: int
    budget_tokens: int


# Rough word->token ratio; good enough for a real-time budget check without
# needing a full tokenizer in the hot path.
_TOKENS_PER_WORD = 1.3


def check_cost(*args, **kwargs) -> CostResult:
    response = ""
    budget_tokens = 60

    for a in args:
        if isinstance(a, str):
            response = a
        elif isinstance(a, int):
            budget_tokens = a
        elif hasattr(a, "cost_budget_tokens"):
            budget_tokens = getattr(a, "cost_budget_tokens")

    if "response" in kwargs:
        response = kwargs["response"]
    if "budget_tokens" in kwargs:
        budget_tokens = kwargs["budget_tokens"]
    elif "policy" in kwargs and hasattr(kwargs["policy"], "cost_budget_tokens"):
        budget_tokens = kwargs["policy"].cost_budget_tokens

    word_count = len(response.split())
    estimated_tokens = math.ceil(word_count * _TOKENS_PER_WORD)
    over_ratio = estimated_tokens / budget_tokens if budget_tokens else 0

    score = 0
    if over_ratio > 1:
        score = min(100, round((over_ratio - 1) * 100))

    return CostResult(score=score, estimated_tokens=estimated_tokens, budget_tokens=budget_tokens)
