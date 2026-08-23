"""Classification stage: (rail, error_code, free_text) -> Category + confidence.

Two tiers, in this order, and the order is the point:

1. **Rule lookup** against `taxonomy.py`. Deterministic, free, instant, and correct
   for the large majority of real traffic, which arrives with a clean Razorpay
   `error_code`. Reaching for a model here would be slower, costlier and *less*
   accurate than a dict lookup.
2. **Self-consistency LLM ensemble** for what the table cannot cover: free-text
   narration forwarded from a bank or PSP, where every bank writes its own wording.

That split is the "AI judgment" claim in concrete form — the model is used where the
rules genuinely run out, and deliberately not used where they don't.

Every failure mode in tier 2 — disagreement, tie, low confidence, API error, safety
refusal, missing credentials — resolves to UNKNOWN, which `compliance.evaluate` maps
to `escalate_human` under COMP-002. Absent an API key the whole tier is inert and the
system behaves exactly as it did before it existed.
"""

from dataclasses import dataclass

from app.models import Category, Rail
from app.taxonomy import lookup


@dataclass
class ClassificationResult:
    category: Category
    confidence: float
    source: str  # "rule" | "llm" | "llm_rejected" | "rule_miss"
    raw: dict | None = None


def classify(rail: Rail, error_code: str, error_description: str = "") -> ClassificationResult:
    category = lookup(rail, error_code)
    if category is not None:
        return ClassificationResult(category=category, confidence=1.0, source="rule")

    llm_result = classify_free_text(rail, error_code, error_description)
    if llm_result is not None:
        return llm_result

    return ClassificationResult(
        category=Category.UNKNOWN,
        confidence=0.0,
        source="rule_miss",
        raw={"rail": rail.value, "error_code": error_code, "error_description": error_description},
    )


def classify_free_text(rail: Rail, error_code: str, error_description: str) -> ClassificationResult | None:
    """Second tier. Returns None when there is nothing to work with, so the caller
    falls through to the same `rule_miss` path it always had.

    A *rejected* ensemble is not None — it returns UNKNOWN with the votes attached,
    because "the model looked at this and could not agree" is materially different
    from "no model ran", and the audit trail should be able to tell them apart.
    """
    from app import llm, llm_classifier  # deferred: keeps the SDK off the import path

    # The unmatched code is itself evidence — some PSPs put the whole narration in the
    # code field — so classify both together rather than only the description.
    narration = " ".join(part for part in (error_code, error_description) if part).strip()
    if not narration or not llm.available():
        return None

    ensemble = llm_classifier.classify(rail, narration)
    if ensemble.accepted:
        return ClassificationResult(
            category=ensemble.category,
            confidence=ensemble.confidence,
            source="llm",
            raw=ensemble.as_audit(),
        )

    return ClassificationResult(
        category=Category.UNKNOWN,
        confidence=0.0,
        source="llm_rejected",
        raw=ensemble.as_audit(),
    )
