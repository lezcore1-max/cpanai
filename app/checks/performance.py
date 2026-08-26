"""
Performance check — hallucination / groundedness detection via LLM-as-judge.

This is the check that can't be done with regex: there's often no reliable
real-time ground truth to verify a claim against, so we use a second,
independent model call to judge whether the response is grounded in the
supplied source context (if any) and internally plausible.

Known limitation (explicitly surfaced): when no source context is provided,
the judge can only assess internal plausibility — not grounding against known
facts. This is the "no ground truth" problem described in the design brief.
It is surfaced explicitly via the `no_context` and `confidence` fields on
PerformanceResult so that downstream consumers, reviewers, and the UI can
treat a high score in that mode with appropriate additional caution rather
than treating it as equivalent to a context-grounded score.

Requires GEMINI_API_KEY. Falls back to a heuristic if the key is absent or
the API call fails — degraded detection is safer than a hard failure.
"""

import json
import os
import re
from dataclasses import dataclass

import google.generativeai as genai
from google.generativeai.types import GenerationConfig

_JUDGE_MODEL = "gemini-3.1-flash-lite"

_JUDGE_PROMPT = """\
You are a strict groundedness auditor. Given a QUESTION, an optional \
SOURCE CONTEXT, and an AI RESPONSE, judge whether the response is \
well-grounded in the context (if given) and factually plausible, or \
likely fabricated / overconfident.

Respond ONLY with compact JSON, no markdown, no preamble:
{{"groundedness_score": <int 0-100, 100=fully grounded, 0=likely hallucinated>, \
"confidence": <"high"|"medium"|"low" — your confidence in this score; \
use "low" or "medium" when no source context was provided since you can \
only judge plausibility, not grounding against facts>, \
"reasoning": "<under 20 words>"}}

QUESTION: {question}
SOURCE CONTEXT: {context}
AI RESPONSE: {response}
"""


@dataclass
class PerformanceResult:
    score: int          # risk score: 100 - groundedness_score
    reasoning: str
    method: str         # "llm-judge" | "heuristic-fallback"
    confidence: str     # "high" | "medium" | "low"
    no_context: bool    # True when no source context was provided.
                        # In that mode the score reflects plausibility only —
                        # not grounding against known facts. Treat with
                        # additional caution; a production system would
                        # lower the effective weight of this check or require
                        # human review for any non-PASS outcome.


def _heuristic_fallback(response: str, no_context: bool = False) -> PerformanceResult:
    overconfident = re.search(
        r"\b(always|guarantee|100%|never fails|automatically|definitely)\b", response, re.I
    )
    score = 55 if overconfident else 20
    return PerformanceResult(
        score=score,
        reasoning="LLM-judge unavailable — fell back to a heuristic scan for overconfident phrasing.",
        method="heuristic-fallback",
        confidence="low",
        no_context=no_context,
    )


async def check_performance(question: str, context: str, response: str) -> PerformanceResult:
    no_context = not bool(context and context.strip())
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _heuristic_fallback(response, no_context=no_context)

    context_text = (
        context if not no_context
        else "(none provided — plausibility-only mode; grounding against verified facts is not possible)"
    )

    prompt = _JUDGE_PROMPT.format(
        question=question or "(none provided)",
        context=context_text,
        response=response,
    )

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(_JUDGE_MODEL)
        gemini_response = await model.generate_content_async(
            prompt,
            generation_config=GenerationConfig(max_output_tokens=200),
        )
        text = gemini_response.text
        cleaned = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)

        groundedness = max(0, min(100, int(parsed.get("groundedness_score", 50))))
        confidence = parsed.get("confidence", "medium")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        # Enforce: without source context the judge is estimating plausibility
        # only, so "high" confidence is not warranted regardless of what the
        # model returns — cap it at "medium".
        if no_context and confidence == "high":
            confidence = "medium"

        reasoning = parsed.get("reasoning", "Judged by secondary model.")
        if no_context:
            reasoning = f"[plausibility-only — no source context provided] {reasoning}"

        return PerformanceResult(
            score=100 - groundedness,
            reasoning=reasoning,
            method="llm-judge",
            confidence=confidence,
            no_context=no_context,
        )
    except (json.JSONDecodeError, KeyError, ValueError, AttributeError, Exception):
        return _heuristic_fallback(response, no_context=no_context)
