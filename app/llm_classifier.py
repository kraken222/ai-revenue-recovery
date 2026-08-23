"""Self-consistency classification of unstructured decline narrations.

Most Razorpay failures arrive with a clean `error_code` and the rule table in
`taxonomy.py` resolves them exactly — no model needed, and no model wanted. This
module handles the residue: free-text narration forwarded from a bank or UPI PSP
("INSUFFICIENT BAL AC XX4471", "MANDATE NOT REGISTERED AT REMITTER BANK") that no
rule table will ever cover exhaustively, because each bank writes its own.

### Why prompt ensembling instead of temperature sampling

Textbook self-consistency samples one prompt N times at temperature > 0 and takes the
majority. That is not available here: `temperature`, `top_p` and `top_k` are removed
on Claude Opus 5 and return a 400. Repeating one identical prompt would therefore
mostly re-measure the same computation and manufacture false agreement — three
identical answers that were never three independent opinions.

So the ensemble varies the *framing* rather than the sampling: each probe presents the
same narration under a different task construction (direct classification, operational
consequence, exclusion). Agreement across framings is a stronger signal than agreement
across seeds — it shows the decision is a property of the evidence rather than of one
prompt's phrasing, and prompt-sensitivity is the failure mode that actually bites in
production.

### The conservative default is load-bearing

Disagreement, a tie, low confidence, an API error, a safety refusal, or no credentials
all resolve to UNKNOWN. `compliance.evaluate` maps UNKNOWN to `escalate_human` under
COMP-002, so every one of those paths ends at a person rather than at a guess. The
model can move a payment between compliant actions; it cannot invent one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app import llm
from app.config import settings
from app.models import Category, Rail

_IN_SCOPE = {Category.SOFT_DECLINE, Category.TECHNICAL, Category.HARD_DECLINE, Category.RISK_BLOCK}

_SYSTEM = """You classify failed recurring-payment declines for an Indian payments \
system into one of four recovery categories. You are given the payment rail and the \
raw narration a bank or PSP returned.

soft_decline  transient and customer-side; the same instrument could succeed later.
              Insufficient funds or balance, per-transaction or velocity limits.
hard_decline  the instrument or mandate itself is dead; retrying it is pointless.
              Expired/invalid/blocked card, cancelled or unregistered mandate, closed
              account. India has no card Account Updater, so a dead card cannot be
              silently repaired — it needs fresh customer authorisation.
technical     infrastructure, not the customer. Issuer or PSP unavailable, NPCI or
              gateway timeout, switch errors.
risk_block    declined by a fraud or risk engine. Never auto-retried.

Judge only from the narration. If it is ambiguous, truncated, or could plausibly be \
two categories, say so with low confidence — a wrong confident answer is far more \
costly here than an admitted uncertainty, because low-confidence results are routed to \
a human while confident ones are acted on."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["soft_decline", "hard_decline", "technical", "risk_block"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
    },
    "required": ["category", "confidence", "reasoning"],
    "additionalProperties": False,
}

# Three framings of one question. Varying the construction is what makes the votes
# more than a single opinion counted three times.
_FRAMINGS: list[tuple[str, str]] = [
    (
        "direct",
        "Rail: {rail}\nDecline narration: {text}\n\n"
        "Classify this decline into one of the four categories.",
    ),
    (
        "consequence",
        "Rail: {rail}\nDecline narration: {text}\n\n"
        "A recovery agent must decide what to do next. Would retrying the SAME "
        "instrument later plausibly succeed, is the instrument dead and in need of "
        "fresh authorisation, is this an infrastructure fault, or did a risk engine "
        "block it? Answer with the matching category.",
    ),
    (
        "exclusion",
        "Rail: {rail}\nDecline narration: {text}\n\n"
        "Rule out the categories that clearly do not apply, then report the one that "
        "remains. If more than one survives, report the closest and lower your "
        "confidence accordingly.",
    ),
]


@dataclass
class EnsembleResult:
    category: Category
    confidence: float
    agreement: float          # share of successful probes backing the winner
    votes: list[dict]
    accepted: bool
    reason: str

    def as_audit(self) -> dict:
        return {
            "category": self.category.value,
            "confidence": round(self.confidence, 3),
            "agreement": round(self.agreement, 3),
            "accepted": self.accepted,
            "reason": self.reason,
            "votes": self.votes,
        }


def _reject(reason: str, votes: list[dict], agreement: float = 0.0) -> EnsembleResult:
    return EnsembleResult(Category.UNKNOWN, 0.0, agreement, votes, False, reason)


def classify(rail: Rail, narration: str, samples: int | None = None) -> EnsembleResult:
    """Run the ensemble. Always returns a result; `accepted` is what the caller acts
    on, and a rejected result still carries its votes, so the audit trail records what
    the model actually said before it was overruled."""
    samples = samples if samples is not None else settings.llm_self_consistency_samples
    framings = _FRAMINGS[: max(1, min(samples, len(_FRAMINGS)))]

    if not narration or not narration.strip():
        return _reject("empty_narration", [])

    votes: list[dict] = []
    for name, template in framings:
        call = llm.complete_json(
            system=_SYSTEM,
            prompt=template.format(rail=rail.value, text=narration.strip()),
            schema=_SCHEMA,
        )
        if not call.ok or not call.data:
            votes.append({"framing": name, "error": call.error})
            continue
        votes.append(
            {
                "framing": name,
                "category": call.data.get("category"),
                "confidence": call.data.get("confidence"),
                "reasoning": (call.data.get("reasoning") or "")[:300],
            }
        )

    usable = [v for v in votes if v.get("category")]
    if not usable:
        return _reject("no_usable_votes", votes)

    tally = Counter(v["category"] for v in usable)
    winner, count = tally.most_common(1)[0]
    agreement = count / len(usable)

    # A tie is not a majority. With an even number of usable votes two categories can
    # both reach the top count, and `most_common` would break it by insertion order —
    # silently turning a coin flip into a decision.
    if sum(1 for n in tally.values() if n == count) > 1:
        return _reject("tied_vote", votes, agreement)

    try:
        category = Category(winner)
    except ValueError:
        return _reject("unrecognised_category", votes, agreement)
    if category not in _IN_SCOPE:
        return _reject("category_out_of_scope", votes, agreement)

    backing = [v for v in usable if v["category"] == winner]
    mean_conf = sum(float(v.get("confidence") or 0.0) for v in backing) / len(backing)

    if agreement < settings.llm_min_agreement:
        return EnsembleResult(category, mean_conf, agreement, votes, False, "below_agreement_floor")
    if mean_conf < settings.llm_min_confidence:
        return EnsembleResult(category, mean_conf, agreement, votes, False, "below_confidence_floor")

    return EnsembleResult(category, mean_conf, agreement, votes, True, "accepted")
